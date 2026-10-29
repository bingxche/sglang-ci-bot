#!/usr/bin/env python3
"""Comment on merged sglang PRs that today's report ties to an AMD CI break.

The Daily Cross-Workflow Summary agent ends its report with a
``suspect-prs`` block naming, per cluster, the merged PR it thinks caused
it and with what confidence. ``daily_cross_workflow_summary.py`` keeps a
validated copy as a hidden comment in the daily issue body. This script,
run once per tick by the ``finalize`` job, reads that copy and decides in
code whether each suspect is solid enough to tell the PR's author:

  - confidence is ``FACT`` or ``HIGH``
  - the cluster has at least ``MIN_FAILED_JOBS`` failed jobs
  - the PR is merged, within ``MAX_PR_AGE_DAYS``, and the cited commit is
    the PR's
  - the commit lies between the last passing and the first failing commit
    the agent cited (checked with the compare API)
  - the PR has no suspect-PR comment yet, for any cluster: cluster IDs are
    reassigned from day to day, so a per-cluster check would comment on the
    same break again under its new ID

When the cluster has a fix PR, the comment instead ``@``-mentions the
suspect PR's author and asks them to review it, once per fix PR, even on a
PR that already had a suspect comment. That needs the fix PR to be open,
by someone else, and related to the suspect PR: it must reference the
suspect PR or change one of its files. A merged fix skips the suspect, and
one closed without merging is treated as no fix. Once a suspect PR's
author was asked to review an open fix, a different fix PR named on a
later tick is not sent.

With no open fix PR named, the suspect is skipped when an open PR references
it: that PR may be a fix the agent missed.

Modes (``--mode`` / ``SUSPECT_PR_COMMENTS``):

  - ``dry-run`` (default): post nothing upstream; record what would be
    posted, and why the rest was skipped, in one comment on the daily issue
  - ``post``: also post on the PR, at most ``MAX_POSTS_PER_RUN`` per tick
  - ``off``: do nothing
"""

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_cross_workflow_summary import (
    collect_workflow_analyses,
    extract_summary_from_body,
    parse_job_cluster_comment,
    parse_suspect_prs_comment,
)
from monitor_ci import find_daily_issue, get_issue_comments
from utils import REPO, get_issue, gh_headers, post_comment, update_comment

log = logging.getLogger("suspect-prs")

MODES = ("off", "dry-run", "post")
DEFAULT_MODE = "dry-run"
POSTABLE_CONFIDENCE = ("FACT", "HIGH")
MIN_FAILED_JOBS = 2
MAX_PR_AGE_DAYS = 14
MAX_POSTS_PER_RUN = 3
MAX_JOBS_LISTED = 8

LOG_COMMENT_MARKER = "<!-- ci-monitor-suspect-pr-log -->"
_PR_MARKER = "<!-- amd-bot-suspect-pr:{cluster} -->"
_PR_MARKER_RE = re.compile(r"<!-- amd-bot-suspect-pr:(R\d+) -->")
# On a comment that asks the suspect PR's author to review an open fix PR.
_FIX_MARKER = "<!-- amd-bot-suspect-fix:{fix_pr} -->"
_FIX_MARKER_RE = re.compile(r"<!-- amd-bot-suspect-fix:(\d+) -->")
MAX_PR_FILE_PAGES = 3
_MENTION_RE = re.compile(r"(?<![\w`])@([A-Za-z0-9][\w-]*(?:/[\w.-]+)?)")


def pr_marker(cluster: str) -> str:
    return _PR_MARKER.format(cluster=cluster)


def fix_marker(fix_pr: int) -> str:
    return _FIX_MARKER.format(fix_pr=fix_pr)


def _pr_url(pr: int) -> str:
    return f"https://github.com/{REPO}/pull/{pr}"


def references_pr(pull: dict, pr: int) -> bool:
    """Whether a PR's title or body names ``pr`` as ``#<pr>`` or by its URL."""
    text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
    return re.search(rf"(?<![\w&])#{pr}\b|/pull/{pr}\b", text) is not None


def _commit_link(sha: str) -> str:
    return f"[`{sha[:8]}`](https://github.com/{REPO}/commit/{sha})"


def neutralize_mentions(text: str) -> str:
    """Put ``@handle`` in code so the comment pings nobody."""
    return _MENTION_RE.sub(r"`@\1`", text)


# ---------------------------------------------------------------------------
# GitHub lookups (a class so tests can swap in a fake)
# ---------------------------------------------------------------------------

class GitHub:
    def __init__(self, token: str):
        self.token = token
        self._comments: dict[int, list[dict]] = {}

    def _get(self, path: str) -> dict | list | None:
        resp = requests.get(
            f"https://api.github.com/repos/{REPO}/{path}", headers=gh_headers(self.token),
        )
        if resp.status_code in (404, 422):
            return None
        resp.raise_for_status()
        return resp.json()

    def pull(self, pr: int) -> dict | None:
        return self._get(f"pulls/{pr}")

    def full_sha(self, sha: str) -> str | None:
        commit = self._get(f"commits/{sha}")
        return commit.get("sha") if commit else None

    def commit_pulls(self, sha: str) -> list[int]:
        return [p["number"] for p in self._get(f"commits/{sha}/pulls") or []]

    def compare_status(self, base: str, head: str) -> str | None:
        result = self._get(f"compare/{base}...{head}")
        return result.get("status") if result else None

    def pr_comments(self, pr: int) -> list[dict]:
        if pr not in self._comments:
            self._comments[pr] = get_issue_comments(self.token, REPO, pr)
        return self._comments[pr]

    def pr_files(self, pr: int) -> set[str]:
        files: set[str] = set()
        for page in range(1, MAX_PR_FILE_PAGES + 1):
            batch = self._get(f"pulls/{pr}/files?per_page=100&page={page}") or []
            files.update(f["filename"] for f in batch)
            if len(batch) < 100:
                break
        return files

    def open_prs_referencing(self, pr: int) -> list[dict]:
        """Open PRs whose title or body names ``pr``, as search results
        (``number``, ``title``, ``body``, ``user``)."""
        resp = requests.get(
            "https://api.github.com/search/issues", headers=gh_headers(self.token), timeout=60,
            params={"q": f"repo:{REPO} is:pr is:open {pr}", "per_page": 30},
        )
        resp.raise_for_status()
        return [i for i in resp.json().get("items", [])
                if i["number"] != pr and references_pr(i, pr)]


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    suspect: dict
    jobs: list[int]
    result: str = ""          # "post", "skip", "already", "posted", "held"
    reason: str = ""
    full_sha: str = ""
    comment_url: str = ""
    body: str = field(default="", repr=False)
    # Set when the comment asks the PR's author to review an open fix PR.
    fix: dict | None = None
    pr_author: str = ""


def check_fix(fix_pr: int, suspect_pr: int, author: str, gh: GitHub) -> tuple[dict | None, str]:
    """``(fix, "")`` when the fix PR is open and worth a review request,
    ``(None, "")`` when it is gone (closed unmerged: comment as if there were
    no fix), or ``(None, reason)`` to skip the suspect."""
    pull = gh.pull(fix_pr)
    if not pull:
        return None, f"fix PR #{fix_pr} not found"
    if pull.get("merged_at"):
        return None, f"fix PR #{fix_pr} already merged"
    if pull.get("state") != "open":
        return None, ""
    fix_author = (pull.get("user") or {}).get("login", "")
    if fix_author.lower() == author.lower():
        return None, f"fix PR #{fix_pr} is by the PR's author"
    # The agent can name a PR whose title matches but whose diff is
    # elsewhere; asking the author to review that wastes their time.
    if not references_pr(pull, suspect_pr) and not gh.pr_files(fix_pr) & gh.pr_files(suspect_pr):
        return None, f"fix PR #{fix_pr} neither references #{suspect_pr} nor changes any of its files"
    return {"number": fix_pr, "title": pull.get("title", ""), "author": fix_author}, ""


def earlier_open_fix(comments: list[dict], fix_pr: int, gh: GitHub) -> tuple[int, dict] | None:
    """An earlier review request on this PR for a different fix PR that is
    still open, so a fix PR changing from tick to tick asks only once."""
    for c in comments:
        m = _FIX_MARKER_RE.search(c.get("body") or "")
        if not m or int(m.group(1)) == fix_pr:
            continue
        pull = gh.pull(int(m.group(1)))
        if pull and pull.get("state") == "open" and not pull.get("merged_at"):
            return int(m.group(1)), c
    return None


def check_suspect(
    suspect: dict, jobs: list[int], gh: GitHub, now: datetime,
) -> Decision:
    """Run every check on one suspect; the first failure is the reason."""
    d = Decision(suspect, jobs)

    def skip(reason: str) -> Decision:
        d.result, d.reason = "skip", reason
        return d

    s = suspect
    if s["confidence"] not in POSTABLE_CONFIDENCE:
        return skip(f"confidence {s['confidence']} (needs FACT or HIGH)")
    if len(jobs) < MIN_FAILED_JOBS:
        return skip(f"{len(jobs)} failed job(s) in {s['cluster']} (needs {MIN_FAILED_JOBS})")
    if not s.get("last_pass_sha") or not s.get("first_fail_sha"):
        return skip("no last-passing / first-failing commit given")

    pull = gh.pull(s["pr"])
    if not pull:
        return skip("PR not found")
    if not pull.get("merged_at"):
        return skip("PR is not merged")
    merged_at = datetime.fromisoformat(pull["merged_at"].replace("Z", "+00:00"))
    age_days = (now - merged_at).days
    if age_days > MAX_PR_AGE_DAYS:
        return skip(f"merged {age_days} days ago (limit {MAX_PR_AGE_DAYS})")

    full = gh.full_sha(s["sha"])
    if not full:
        return skip(f"commit {s['sha'][:8]} not found")
    d.full_sha = full
    if pull.get("merge_commit_sha") != full and s["pr"] not in gh.commit_pulls(full):
        return skip(f"commit {full[:8]} is not from #{s['pr']}")

    after_pass = gh.compare_status(s["last_pass_sha"], full) == "ahead"
    before_fail = gh.compare_status(full, s["first_fail_sha"]) in ("ahead", "identical")
    if not (after_pass and before_fail):
        return skip(
            f"commit {full[:8]} is not between last passing {s['last_pass_sha'][:8]} "
            f"and first failing {s['first_fail_sha'][:8]}"
        )

    d.pr_author = (pull.get("user") or {}).get("login", "")
    if s.get("fix_pr"):
        d.fix, reason = check_fix(s["fix_pr"], s["pr"], d.pr_author, gh)
        if reason:
            return skip(reason)

    if d.fix:
        # One review request per fix PR, even after an earlier suspect comment.
        marker = fix_marker(d.fix["number"])
        comments = gh.pr_comments(s["pr"])
        for c in comments:
            if marker in (c.get("body") or ""):
                d.result, d.comment_url = "already", c.get("html_url", "")
                d.reason = f"asked to review #{d.fix['number']}"
                return d
        earlier = earlier_open_fix(comments, d.fix["number"], gh)
        if earlier:
            d.result, d.comment_url = "already", earlier[1].get("html_url", "")
            d.reason = f"asked to review #{earlier[0]}, still open; not asking about #{d.fix['number']}"
            return d
        d.result = "post"
        return d

    for c in gh.pr_comments(s["pr"]):
        m = _PR_MARKER_RE.search(c.get("body") or "")
        if m:
            d.result, d.comment_url = "already", c.get("html_url", "")
            if m.group(1) != s["cluster"]:
                d.reason = f"commented earlier as {m.group(1)}"
            return d

    # A PR that links the suspect is often a fix the agent's search missed.
    referencing = gh.open_prs_referencing(s["pr"])
    if referencing:
        nums = ", ".join(f"#{p['number']}" for p in referencing[:3])
        return skip(f"no open fix PR named, but open PR {nums} references #{s['pr']}")

    d.result = "post"
    return d


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_pr_comment(d: Decision, job_info: dict[int, dict]) -> str:
    """The comment posted on the suspect PR."""
    s = d.suspect
    if d.fix:
        # The one deliberate mention: the author is asked to review the fix.
        lines = [
            fix_marker(d.fix["number"]),
            f"@{d.pr_author} AMD CI jobs started failing after this PR was merged, and "
            f"#{d.fix['number']} proposes a fix. Could you review it?",
            "",
        ]
    else:
        lines = [
            pr_marker(s["cluster"]),
            "AMD CI jobs started failing after this PR was merged, and the evidence points at this change.",
            "",
        ]
    if s.get("evidence"):
        lines += [neutralize_mentions(s["evidence"]), ""]
    window = (
        f"The last passing run was on {_commit_link(s['last_pass_sha'])} and the first "
        f"failing run on {_commit_link(s['first_fail_sha'])}; this PR's commit "
        f"{_commit_link(d.full_sha or s['sha'])} is between them."
    )
    if s.get("related_prs"):
        window += " Also involved: " + ", ".join(f"#{n}" for n in s["related_prs"]) + "."
    lines += [window, "", f"Failing jobs ({len(d.jobs)}):"]

    # Take jobs from each run in turn, so the list shows how far the break spreads.
    by_run: dict[str, list[int]] = {}
    for j in d.jobs:
        run_url = job_info.get(j, {}).get("run_url")
        if run_url:
            by_run.setdefault(run_url, []).append(j)
    listed = [j for turn in zip_longest(*by_run.values()) for j in turn if j is not None]
    listed = listed[:MAX_JOBS_LISTED]
    for j in listed:
        info = job_info[j]
        url = f"{info['run_url'].rstrip('/')}/job/{j}"
        name = (info.get("job_name") or str(j)).replace("[", "(").replace("]", ")")
        lines.append(f"- [{name}]({url})")
    if len(d.jobs) > len(listed):
        lines.append(f"- …and {len(d.jobs) - len(listed)} more")
    if d.fix:
        closing = (
            "If this PR isn't the cause, please say so here so the attribution can be corrected."
        )
    else:
        closing = (
            "If this PR is the cause, a fix or a revert would turn these jobs green again. "
            "If it isn't, please say so here so the attribution can be corrected."
        )
    lines += [
        "",
        closing,
        "",
        # Says amd-bot even while another account's token does the posting.
        f"<sub>Posted automatically by [amd-bot](https://github.com/amd-bot), the AMD CI monitor. "
        f"Confidence: {s['confidence']}.</sub>",
    ]
    return "\n".join(lines)


_RESULT_TEXT = {
    "post": "would comment",
    "posted": "commented",
    "already": "already commented",
    "held": f"held: over the limit of {MAX_POSTS_PER_RUN} comments per tick",
}


def render_log_comment(decisions: list[Decision], mode: str, snapshot_utc: str) -> str:
    """The comment on the daily issue recording every decision."""
    title = "Suspect PR comments" + (" (dry run)" if mode == "dry-run" else "")
    if mode == "dry-run":
        note = (
            "Nothing is posted on sglang PRs in this mode. Set the repository variable "
            "`SUSPECT_PR_COMMENTS=post` to post."
        )
    else:
        note = f"Comments are posted on sglang PRs, at most {MAX_POSTS_PER_RUN} per tick."
    lines = [
        LOG_COMMENT_MARKER,
        f"## {title}",
        "",
        f"{note} Last updated {snapshot_utc}.",
        "",
    ]
    if not decisions:
        lines.append("No suspect PRs in today's summary.")
        return "\n".join(lines)

    lines += [
        "| Cluster | PR | Confidence | Failed jobs | Result |",
        "|---|---|---|---|---|",
    ]
    for d in decisions:
        s = d.suspect
        result = _RESULT_TEXT.get(d.result, f"skipped: {d.reason}")
        if d.result == "already" and d.reason:
            result += f" ({d.reason})"
        elif d.fix and d.result in ("post", "posted", "held"):
            result += f" (review request for #{d.fix['number']})"
        if d.comment_url:
            result = f"[{result}]({d.comment_url})"
        lines.append(
            f"| {s['cluster']} | [#{s['pr']}]({_pr_url(s['pr'])}) | {s['confidence']} "
            f"| {len(d.jobs)} | {result} |"
        )
    for d in decisions:
        if d.body and d.result in ("post", "held"):
            lines += [
                "",
                f"<details><summary>Comment for #{d.suspect['pr']} "
                f"({d.suspect['cluster']})</summary>",
                "",
                d.body,
                "",
                "</details>",
            ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def decide_all(
    suspects: list[dict],
    clusters: dict[str, list[int]],
    job_info: dict[int, dict],
    gh: GitHub,
    now: datetime,
) -> list[Decision]:
    """Check every suspect, strongest first; one decision per (PR, cluster)."""
    seen: set[tuple[int, str]] = set()
    decisions: list[Decision] = []
    for s in suspects:
        key = (s["pr"], s["cluster"])
        if key in seen:
            continue
        seen.add(key)
        jobs = sorted(clusters.get(s["cluster"]) or [])
        try:
            d = check_suspect(s, jobs, gh, now)
        except requests.RequestException as exc:
            d = Decision(s, jobs, result="skip", reason=f"GitHub API error ({exc.__class__.__name__})")
        if d.result == "post":
            d.body = render_pr_comment(d, job_info)
        decisions.append(d)
    rank = {label: i for i, label in enumerate(POSTABLE_CONFIDENCE)}
    decisions.sort(key=lambda d: (
        d.result != "post", rank.get(d.suspect["confidence"], 9), -len(d.jobs),
    ))
    # Two clusters naming the same PR in one tick: only the strongest is posted.
    posting: dict[int, str] = {}
    for d in decisions:
        if d.result != "post":
            continue
        pr = d.suspect["pr"]
        if pr in posting:
            d.result, d.reason, d.body = "skip", f"#{pr} gets one comment, for {posting[pr]}", ""
        else:
            posting[pr] = d.suspect["cluster"]
    return decisions


def apply_decisions(token: str, decisions: list[Decision], post: bool) -> None:
    """Post the passing decisions (``post``), or only apply the per-tick cap."""
    posted = 0
    for d in decisions:
        if d.result != "post":
            continue
        if posted >= MAX_POSTS_PER_RUN:
            d.result = "held"
            continue
        if not post:
            posted += 1
            continue
        if posted:
            time.sleep(1)  # GitHub asks for >= 1s between content writes
        try:
            comment = post_comment(token, REPO, d.suspect["pr"], d.body)
        except requests.RequestException as exc:
            d.result, d.reason = "skip", f"post failed ({exc.__class__.__name__})"
            log.warning("Comment on #%d failed (%s)", d.suspect["pr"], exc)
            continue
        d.result, d.comment_url = "posted", comment.get("html_url", "")
        posted += 1
        log.info("Commented on #%d for %s: %s", d.suspect["pr"], d.suspect["cluster"], d.comment_url)


def upsert_log_comment(token: str, bot_repo: str, issue_num: int, body: str, create: bool) -> None:
    for c in get_issue_comments(token, bot_repo, issue_num):
        if LOG_COMMENT_MARKER in (c.get("body") or ""):
            if c["body"] != body:
                update_comment(token, bot_repo, c["id"], body)
            return
    if create:
        post_comment(token, bot_repo, issue_num, body)


def run(token: str, bot_repo: str, mode: str, date_str: str | None = None) -> int:
    if mode == "off":
        log.info("SUSPECT_PR_COMMENTS=off; nothing to do")
        return 0
    now = datetime.now(timezone.utc)
    date_str = date_str or now.strftime("%Y-%m-%d")
    issue_num = find_daily_issue(token, bot_repo, date_str)
    if not issue_num:
        log.info("No daily issue for %s", date_str)
        return 0

    summary = extract_summary_from_body(get_issue(token, bot_repo, issue_num).get("body", ""))
    suspects = parse_suspect_prs_comment(summary or "")
    clusters = parse_job_cluster_comment(summary or "") or {}
    job_info: dict[int, dict] = {}
    if suspects:
        for jas in collect_workflow_analyses(token, bot_repo, issue_num).values():
            for ja in jas:
                job_info[ja["job_id"]] = ja

    gh = GitHub(token)
    decisions = decide_all(suspects, clusters, job_info, gh, now)
    apply_decisions(token, decisions, post=mode == "post")
    for d in decisions:
        log.info("%s #%d: %s %s", d.suspect["cluster"], d.suspect["pr"], d.result, d.reason)

    body = render_log_comment(decisions, mode, now.strftime("%Y-%m-%d %H:%M UTC"))
    upsert_log_comment(token, bot_repo, issue_num, body, create=bool(decisions))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bot-repo", required=True, help="Bot repo, e.g. bingxche/sglang-ci-bot")
    parser.add_argument("--date", help="Daily issue date (YYYY-MM-DD UTC). Defaults to today.")
    parser.add_argument(
        "--mode", choices=MODES,
        default=(os.environ.get("SUSPECT_PR_COMMENTS") or DEFAULT_MODE).strip().lower(),
        help=f"off / dry-run / post (default: SUSPECT_PR_COMMENTS or {DEFAULT_MODE})",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("BOT_PAT", os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", ""))),
        help="GitHub token (default: BOT_PAT / GH_PAT / GITHUB_TOKEN)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )
    if not args.github_token:
        log.error("GitHub token required. Set GH_PAT / BOT_PAT / GITHUB_TOKEN.")
        return 1
    mode = args.mode
    if mode not in MODES:
        log.warning("Unknown SUSPECT_PR_COMMENTS=%r; using %s", mode, DEFAULT_MODE)
        mode = DEFAULT_MODE
    return run(args.github_token, args.bot_repo, mode, args.date)


if __name__ == "__main__":
    sys.exit(main())
