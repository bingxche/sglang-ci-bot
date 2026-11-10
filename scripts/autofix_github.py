"""ci-autofix's GitHub and git access: reads with any token, writes (push,
dispatch, draft PR, branch deletion) only with ``AUTOFIX_TOKEN`` and only on
``BRANCH_PREFIX`` branches (``check_branch()``)."""

import base64
import collections
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

from autofix_config import (
    API,
    ARTIFACT_NAME_RE,
    AUTOFIX_LOGIN,
    BRANCH_PREFIX,
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_FILE_CHARS,
    MAX_ARTIFACT_FILES,
    MAX_ARTIFACT_TOTAL_CHARS,
    MAX_OPEN_PRS_SCANNED,
)
from utils import (
    REPO,
    _download_artifact_archive,
    download_job_logs,
    get_pr_changed_files,
    get_pr_diff,
    get_run_jobs,
    gh_get,
    gh_headers,
)

# Reads that hang would hold the hourly job (and every tick queued behind it).
TIMEOUT = 60


class GitHub:
    """sglang reads (amd-bot's token) and writes (``AUTOFIX_TOKEN``); tests swap in a fake."""

    def __init__(self, token: str, write_token: str = ""):
        self.token = token
        self.write_token = write_token
        self._runs: dict[int, dict] = {}
        self._jobs: dict[int, list[dict]] = {}
        self._logs: dict[int, str] = {}
        self._scheduled: dict[str, list[dict]] = {}
        self._files: dict[int, list[dict]] = {}
        self._artifacts: dict[tuple, dict[str, str]] = {}
        self._dispatched: set[str] = set()  # workflows this process dispatched on main

    # reads ---------------------------------------------------------------
    def _get(self, endpoint: str, **params):
        resp = gh_get(f"{API}/{endpoint}", headers=gh_headers(self.token), params=params or None, timeout=TIMEOUT)
        if resp.status_code in (404, 422):
            return None
        resp.raise_for_status()
        return resp.json()

    def run(self, run_id: int, fresh: bool = False) -> dict:
        if fresh or run_id not in self._runs:
            self._runs[run_id] = self._get(f"actions/runs/{run_id}") or {}
        return self._runs[run_id]

    def jobs(self, run_id: int, fresh: bool = False) -> list[dict]:
        if fresh or run_id not in self._jobs:
            self._jobs[run_id] = get_run_jobs(self.token, run_id)
        return self._jobs[run_id]

    def job_log(self, job_id: int) -> str:
        """A job's log; a failed download (``LOG_UNAVAILABLE``) is not kept, so a later call tries again."""
        if job_id in self._logs:
            return self._logs[job_id]
        text = download_job_logs(self.token, job_id)
        if not text.startswith(LOG_UNAVAILABLE):
            self._logs[job_id] = text
        return text

    def scheduled_runs(self, workflow: str) -> list[dict]:
        """Completed scheduled runs of *workflow* on main, newest first."""
        if workflow not in self._scheduled:
            data = self._get(f"actions/workflows/{workflow}/runs", event="schedule", branch="main",
                             status="completed", per_page=30) or {}
            self._scheduled[workflow] = data.get("workflow_runs") or []
        return self._scheduled[workflow]

    def search_prs(self, query: str) -> list[dict]:
        resp = gh_get("https://api.github.com/search/issues", headers=gh_headers(self.token),
                      params={"q": f"repo:{REPO} is:pr {query}", "per_page": 50}, timeout=TIMEOUT)
        if resp.status_code == 422:
            return []
        resp.raise_for_status()
        return resp.json().get("items") or []

    def open_prs(self, since: str) -> list[dict]:
        """Open PRs created on or after *since* (YYYY-MM-DD), newest first."""
        prs: list[dict] = []
        page = 1
        while len(prs) < MAX_OPEN_PRS_SCANNED:
            batch = self._get("pulls", state="open", sort="created", direction="desc",
                              per_page=100, page=page) or []
            prs += [p for p in batch if p["created_at"][:10] >= since]
            if len(batch) < 100 or batch[-1]["created_at"][:10] < since:
                break
            page += 1
        return prs[:MAX_OPEN_PRS_SCANNED]

    def pr_files(self, number: int) -> list[dict]:
        if number not in self._files:
            self._files[number] = get_pr_changed_files(self.token, number)
        return self._files[number]

    def commits_touching(self, path: str, since: str) -> list[dict]:
        """Commits on main that changed *path* after *since* (an ISO time)."""
        return self._get("commits", path=path, since=since, sha="main", per_page=50) or []

    def pull(self, number: int) -> dict | None:
        return self._get(f"pulls/{number}")

    def compare(self, base: str, head: str) -> list[dict]:
        data = self._get(f"compare/{base}...{head}") or {}
        return data.get("commits") or []

    def pr_diff(self, number: int) -> str:
        return get_pr_diff(self.token, number)

    def branch_runs(self, workflow: str, branch: str) -> list[dict]:
        data = self._get(f"actions/workflows/{workflow}/runs", branch=branch,
                         event="workflow_dispatch", per_page=20) or {}
        return data.get("workflow_runs") or []

    def log_artifacts(self, run_id: int, rocm: str = "", prefer: str = "") -> dict[str, str]:
        """The text files of a run's log-like artifacts (``ARTIFACT_NAME_RE``), as ``{path: text}``.

        Tests that start servers often keep the server's log only in an
        artifact; the job log then shows just the client side. Artifacts
        named for another ROCm version than *rocm* are left out, and those
        whose name holds *prefer* (the failed job) come first. Log tarballs
        inside an artifact (the disagg servers') are read too. Each file
        keeps only its tail, read as a stream, and the file count and total
        are capped. Kept per run, since a plan's jobs often share one.
        """
        key = (run_id, rocm, prefer)
        if key in self._artifacts:
            return self._artifacts[key]
        artifacts: list[dict] = []
        for page in range(1, 11):
            batch = (self._get(f"actions/runs/{run_id}/artifacts", per_page=100, page=page) or {}).get("artifacts") or []
            artifacts += batch
            if len(batch) < 100:
                break
        wanted = [a for a in artifacts
                  if not a.get("expired") and re.search(ARTIFACT_NAME_RE, a.get("name", ""), re.I)
                  and not (rocm and re.search(r"rocm\d+", a["name"]) and rocm not in a["name"])
                  and a.get("size_in_bytes", 0) <= MAX_ARTIFACT_BYTES]
        wanted.sort(key=lambda a: not (prefer and prefer in a["name"]))
        out: dict[str, str] = {}
        total = 0
        with tempfile.TemporaryDirectory() as tmp:
            for artifact in wanted:
                if total >= MAX_ARTIFACT_TOTAL_CHARS or len(out) >= MAX_ARTIFACT_FILES:
                    break
                zip_path = Path(tmp) / "a.zip"
                _download_artifact_archive(self.token, artifact, zip_path)
                for path, text in _artifact_texts(zip_path):
                    if total + len(text) <= MAX_ARTIFACT_TOTAL_CHARS and len(out) < MAX_ARTIFACT_FILES:
                        out[f"{artifact['name']}/{path}"] = text
                        total += len(text)
        self._artifacts[key] = out
        return out

    def dispatch_busy(self, workflow: str) -> bool:
        """Whether a hand-dispatched run of *workflow* is queued or running: dispatching
        another into its concurrency group could cancel it. A run this process dispatched
        counts too, since GitHub may not list it yet."""
        if workflow in self._dispatched:
            return True
        data = self._get(f"actions/workflows/{workflow}/runs", event="workflow_dispatch", per_page=10) or {}
        return any(r.get("status") in ("queued", "in_progress", "waiting", "pending", "requested")
                   for r in data.get("workflow_runs") or [])

    def pr_for_branch(self, branch: str, sha: str) -> dict | None:
        """The PR already opened from *branch* at commit *sha* (a tick that died after opening it);
        an older PR from a branch of the same name does not count."""
        prs = self._get("pulls", head=f"{REPO.split('/')[0]}:{branch}", state="all", per_page=10) or []
        return next((p for p in prs if (p.get("head") or {}).get("sha") == sha), None)

    # writes (AUTOFIX_TOKEN) ------------------------------------------------
    def _write(self, method: str, path: str, **kwargs) -> requests.Response:
        if not self.write_token:
            raise RuntimeError("AUTOFIX_TOKEN is not set")
        resp = requests.request(method, f"{API}/{path}", headers=gh_headers(self.write_token),
                                timeout=60, **kwargs)
        resp.raise_for_status()
        return resp

    def identity(self) -> tuple[str, str]:
        """(login, noreply email) of the token's owner, for commits."""
        resp = gh_get("https://api.github.com/user", headers=gh_headers(self.write_token), timeout=TIMEOUT)
        resp.raise_for_status()
        user = resp.json()
        return user["login"], f"{user['id']}+{user['login']}@users.noreply.github.com"

    def dispatch(self, branch: str, sha: str, recipe: dict) -> None:
        """Run *recipe* on *branch*: on the branch itself, or on main with the branch's commit
        *sha* in the recipe's ``ref_input`` (the Crusoe workflow). The commit, not the branch
        name: checking out a commit logs ``HEAD is now at <sha>``, which ``verify_run()`` reads.
        Nothing but an autofix branch's commit is ever run."""
        check_branch(branch)
        ref = recipe.get("dispatch_ref") or branch
        inputs = dict(recipe["inputs"])
        if ref != branch:
            if ref != "main" or not recipe.get("ref_input") or not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
                raise RuntimeError(f"refusing to dispatch on {ref!r} without the autofix commit as an input")
            inputs[recipe["ref_input"]] = sha
        self._write("POST", f"actions/workflows/{recipe['workflow']}/dispatches", json={"ref": ref, "inputs": inputs})
        if ref != branch:
            self._dispatched.add(recipe["workflow"])

    def create_draft_pr(self, title: str, body: str, branch: str) -> dict:
        check_branch(branch)
        return self._write("POST", "pulls", json={"title": title, "body": body, "head": branch,
                                                  "base": "main", "draft": True}).json()

    def add_label_if_exists(self, number: int, label: str) -> bool:
        if self._get(f"labels/{label}") is None:
            return False
        self._write("POST", f"issues/{number}/labels", json={"labels": [label]})
        return True

    def delete_branch(self, branch: str) -> None:
        check_branch(branch)
        resp = requests.delete(f"{API}/git/refs/heads/{branch}", headers=gh_headers(self.write_token), timeout=60)
        if resp.status_code not in (204, 404, 422):
            resp.raise_for_status()


LOG_UNAVAILABLE = "[Failed to fetch logs"  # how download_job_logs() reports a failed download


def _tail(stream, size: int) -> tuple[bytes, bytes]:
    """The first 4 KB and the last *size* bytes of *stream*, read in chunks (a log may be GBs)."""
    head = stream.read(4096)
    chunks, kept = collections.deque([head]), len(head)
    while chunk := stream.read(1 << 20):
        chunks.append(chunk)
        kept += len(chunk)
        while len(chunks) > 1 and kept - len(chunks[0]) >= size:
            kept -= len(chunks.popleft())
    return head, b"".join(chunks)[-size:]


def _text_or_none(stream) -> str | None:
    head, tail = _tail(stream, MAX_ARTIFACT_FILE_CHARS * 4)
    if b"\0" in head:  # binary
        return None
    return tail.decode(errors="replace")[-MAX_ARTIFACT_FILE_CHARS:]


def _artifact_texts(zip_path: Path):
    """``(path, tail)`` for the text files of an artifact zip, and of log tarballs inside it."""
    with zipfile.ZipFile(zip_path) as zf:
        for member in sorted(zf.infolist(), key=lambda m: m.filename):
            if member.is_dir():
                continue
            if member.filename.endswith((".tar.gz", ".tgz", ".tar")):
                with zf.open(member) as raw, tarfile.open(fileobj=raw, mode="r|*") as tf:
                    for t in tf:
                        src = tf.extractfile(t) if t.isfile() else None
                        text = _text_or_none(src) if src else None
                        if text is not None:
                            yield f"{member.filename}/{t.name.lstrip('./')}", text
                continue
            with zf.open(member) as src:
                text = _text_or_none(src)
            if text is not None:
                yield member.filename, text


def check_branch(branch: str) -> None:
    if not branch.startswith(BRANCH_PREFIX):
        raise RuntimeError(f"refusing to touch {branch!r}: autofix branches start with {BRANCH_PREFIX}")


def git_bytes(wt: Path, *args: str, timeout: int = 300, check: bool = True, env: dict | None = None,
              input: bytes | None = None) -> bytes:
    """git, in bytes: a patch must reach the publish job byte for byte (CRLF, non-UTF-8 text)."""
    result = subprocess.run(["git", *args], cwd=wt, capture_output=True, timeout=timeout, env=env, input=input)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()[:300]}")
    return result.stdout


def git(wt: Path, *args: str, timeout: int = 300, check: bool = True, env: dict | None = None,
        input: bytes | None = None) -> str:
    return git_bytes(wt, *args, timeout=timeout, check=check, env=env, input=input).decode(errors="replace")


def staged_diff(wt: Path) -> str:
    """Everything the agent changed, new files included, without .ci-context (for reading)."""
    return staged_patch(wt).decode(errors="replace")


def staged_patch(wt: Path) -> bytes:
    """The same, as the binary patch the publish job applies."""
    git(wt, "add", "-A", "--", ".", ":(exclude).ci-context")
    patch = git_bytes(wt, "diff", "--cached", "--binary")
    git(wt, "reset", "-q")
    return patch


def clean_git_env(home: Path) -> dict:
    """Git with no user or system config and no token in the environment.

    The fix job's agents run code on the same runners, so the home directory
    and the shared clone may hold hooks or config (a filter, an fsmonitor)
    that would run during the publish job's commit.
    """
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


def clean_checkout(repo: Path, sha: str, env: dict) -> None:
    """A fresh clone holding only *sha*, for the publish job."""
    repo.mkdir(parents=True)
    git(repo, "init", "-q", env=env)
    git(repo, "fetch", "-q", "--depth", "1", f"https://github.com/{REPO}.git", sha, timeout=900, env=env)
    git(repo, "-c", "core.hooksPath=/dev/null", "checkout", "-q", "--detach", "FETCH_HEAD", env=env)


def push_branch(wt: Path, branch: str, message: str, gh: GitHub, env: dict | None = None) -> str:
    """Commit the worktree's staged changes as the token's owner and push *branch*; returns the commit SHA.
    No hook runs: the commit is made with ``core.hooksPath=/dev/null`` and ``--no-verify``."""
    check_branch(branch)
    login, email = gh.identity()
    if login != AUTOFIX_LOGIN:
        raise RuntimeError(f"AUTOFIX_TOKEN belongs to {login}, not {AUTOFIX_LOGIN}; refusing to push")
    git(wt, "-c", "core.hooksPath=/dev/null", "-c", f"user.name={login}", "-c", f"user.email={email}",
        "commit", "-q", "--no-verify", "-m", message, env=env)
    sha = git(wt, "rev-parse", "HEAD", env=env).strip()
    auth = base64.b64encode(f"x-access-token:{gh.write_token}".encode()).decode()
    git(wt, "-c", "core.hooksPath=/dev/null", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}",
        "push", "--no-verify", "--force", f"https://github.com/{REPO}.git", f"HEAD:refs/heads/{branch}",
        timeout=600, env=env)
    return sha


def find_dispatched_run(gh: GitHub, recipe: dict, branch: str, sha: str, after: datetime,
                        started: datetime | None = None) -> dict | None:
    """The run our dispatch started. On the branch: by commit. On main (Crusoe): the first run
    the token's owner dispatched between *started* (just before our dispatch) and shortly
    after *after* (when it returned); ``verify_run()`` then checks the commit in its log. The
    window is narrow because the owner also dispatches that workflow by hand."""
    def created(run: dict) -> datetime:
        return datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))

    if recipe.get("dispatch_ref"):
        low = (started or after - timedelta(minutes=2)) - timedelta(seconds=30)
        mine = [r for r in gh.branch_runs(recipe["workflow"], recipe["dispatch_ref"])
                if (r.get("actor") or {}).get("login") == AUTOFIX_LOGIN
                and low <= created(r) <= after + timedelta(minutes=2)]
        return min(mine, key=created, default=None)
    for run in gh.branch_runs(recipe["workflow"], branch):
        if run.get("head_sha") == sha and created(run) >= after - timedelta(minutes=2):
            return run
    return None


REPO_LOCK = threading.Lock()
