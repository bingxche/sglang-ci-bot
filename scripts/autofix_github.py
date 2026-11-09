"""ci-autofix's GitHub and git access: reads with any token, writes (push,
dispatch, draft PR, branch deletion) only with ``AUTOFIX_TOKEN`` and only on
``BRANCH_PREFIX`` branches (``check_branch()``)."""

import base64
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

import requests

from autofix_config import API, AUTOFIX_LOGIN, BRANCH_PREFIX, MAX_OPEN_PRS_SCANNED
from utils import REPO, download_job_logs, get_pr_diff, get_run_jobs, gh_get, gh_headers


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

    # reads ---------------------------------------------------------------
    def _get(self, endpoint: str, **params):
        resp = gh_get(f"{API}/{endpoint}", headers=gh_headers(self.token), params=params or None)
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
        if job_id not in self._logs:
            self._logs[job_id] = download_job_logs(self.token, job_id)
        return self._logs[job_id]

    def scheduled_runs(self, workflow: str) -> list[dict]:
        """Completed scheduled runs of *workflow* on main, newest first."""
        if workflow not in self._scheduled:
            data = self._get(f"actions/workflows/{workflow}/runs", event="schedule", branch="main",
                             status="completed", per_page=30) or {}
            self._scheduled[workflow] = data.get("workflow_runs") or []
        return self._scheduled[workflow]

    def search_prs(self, query: str) -> list[dict]:
        resp = gh_get("https://api.github.com/search/issues", headers=gh_headers(self.token),
                      params={"q": f"repo:{REPO} is:pr {query}", "per_page": 50})
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
            self._files[number] = self._get(f"pulls/{number}/files", per_page=100) or []
        return self._files[number]

    def commits_touching(self, path: str, since: str) -> list[dict]:
        return self._get("commits", path=path, since=f"{since}T00:00:00Z", sha="main", per_page=50) or []

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

    def pr_for_branch(self, branch: str) -> dict | None:
        """A PR already opened from *branch* (a tick that died after opening it)."""
        prs = self._get("pulls", head=f"{REPO.split('/')[0]}:{branch}", state="all", per_page=5) or []
        return prs[0] if prs else None

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
        resp = gh_get("https://api.github.com/user", headers=gh_headers(self.write_token))
        resp.raise_for_status()
        user = resp.json()
        return user["login"], f"{user['id']}+{user['login']}@users.noreply.github.com"

    def dispatch(self, workflow: str, branch: str, inputs: dict) -> None:
        check_branch(branch)
        self._write("POST", f"actions/workflows/{workflow}/dispatches", json={"ref": branch, "inputs": inputs})

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


def check_branch(branch: str) -> None:
    if not branch.startswith(BRANCH_PREFIX):
        raise RuntimeError(f"refusing to touch {branch!r}: autofix branches start with {BRANCH_PREFIX}")


def git(wt: Path, *args: str, timeout: int = 300, check: bool = True, env: dict | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=wt, capture_output=True, text=True, timeout=timeout,
                            env=env)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def staged_diff(wt: Path, binary: bool = False) -> str:
    """Everything the agent changed, new files included, without .ci-context."""
    git(wt, "add", "-A", "--", ".", ":(exclude).ci-context")
    diff = git(wt, "diff", "--cached", *(["--binary"] if binary else []))
    git(wt, "reset", "-q")
    return diff


def push_branch(wt: Path, branch: str, message: str, gh: GitHub) -> str:
    """Commit the worktree's staged changes as the token's owner and push *branch*; returns the commit SHA."""
    check_branch(branch)
    login, email = gh.identity()
    if login != AUTOFIX_LOGIN:
        raise RuntimeError(f"AUTOFIX_TOKEN belongs to {login}, not {AUTOFIX_LOGIN}; refusing to push")
    git(wt, "-c", f"user.name={login}", "-c", f"user.email={email}", "commit", "-q", "-m", message)
    sha = git(wt, "rev-parse", "HEAD").strip()
    auth = base64.b64encode(f"x-access-token:{gh.write_token}".encode()).decode()
    git(wt, "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}",
        "push", "--force", f"https://github.com/{REPO}.git", f"HEAD:refs/heads/{branch}", timeout=600)
    return sha


def find_dispatched_run(gh: GitHub, workflow: str, branch: str, sha: str, after: datetime) -> dict | None:
    for run in gh.branch_runs(workflow, branch):
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if run.get("head_sha") == sha and created >= after - timedelta(minutes=2):
            return run
    return None


REPO_LOCK = threading.Lock()
