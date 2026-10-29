#!/usr/bin/env python3
"""
amd-bot PR Review for sglang.

Agent mode (the default) runs a multi-agent review:

  1. Reviewer agents (``Task: PR Code Review``) read the PR in parallel,
     each from one angle: correctness, AMD/ROCm, contracts, and tests and
     conventions. A small PR gets a single reviewer (the quick tier).
  2. Code checks every finding's location and category, merges duplicates,
     and leaves out findings an earlier review round already posted
     (``review_findings.py``).
  3. Independent verifier agents (``Task: PR Code Review Verification``)
     vote on every finding; only findings enough of them confirm are kept.
  4. The result is posted as one PR review: inline comments tagged
     Important / Nit / Pre-existing, and a summary that starts with a
     count. It comments only; it never approves or requests changes.

Every agent gets the sglang maintainers' own review standards, the
``.claude/rules`` and ``.agents/skills`` on sglang ``main``.

API mode (``USE_AGENT=false``) sends the diff to Claude in one call.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from review_findings import (
    DUPLICATE_REASON,
    MAX_NITS,
    Finding,
    cap_findings,
    check_findings,
    count_line,
    diff_hunk_lines,
    drop_repeats,
    fingerprint,
    merge_findings,
    parse_findings,
    parse_marker,
    parse_votes,
    render_inline,
    render_listed,
    render_marker,
    tally,
)
from utils import (
    CLAUDE_MODEL,
    REPO,
    agent_failure_notice,
    checkout_commit,
    claude_code_analyze,
    claude_code_available,
    collect_ci_runtime_env,
    create_agent_worktree,
    create_anthropic_client,
    ensure_sglang_repo,
    gh_headers,
    get_pr_changed_files,
    load_prompt_template,
    post_comment,
    remove_agent_worktree,
)

MAX_DIFF_CHARS = 120000
MAX_AGENT_DIFF_CHARS = 2_000_000

# Review tiers, from the PR's size: one reviewer for small PRs, one per angle otherwise.
ANGLES = ("correctness", "amd", "contracts", "tests-conventions")
TIERS = ("quick", "standard")
QUICK_MAX_LINES = 200
QUICK_MAX_FILES = 6
# After this many rounds the summary asks the author to step back.
MAX_ROUNDS = 3


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


REVIEW_PARALLEL = _env_int("REVIEW_PARALLEL", 4)
REVIEW_VOTERS = _env_int("REVIEW_VOTERS", 3)
REVIEW_VOTES_REQUIRED = _env_int("REVIEW_VOTES_REQUIRED", 2)
REVIEW_MAX_FINDINGS = _env_int("REVIEW_MAX_FINDINGS", 12)
FIND_TIMEOUT_SECS = _env_int("REVIEW_FIND_TIMEOUT_SECS", 1500)
FIND_MAX_TURNS = _env_int("REVIEW_FIND_MAX_TURNS", 200)
VERIFY_TIMEOUT_SECS = _env_int("REVIEW_VERIFY_TIMEOUT_SECS", 900)
VERIFY_MAX_TURNS = _env_int("REVIEW_VERIFY_MAX_TURNS", 80)


def get_pr_info(token: str, pr_number: int) -> dict:
    """Get PR metadata."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}"
    resp = requests.get(url, headers=gh_headers(token))
    resp.raise_for_status()
    return resp.json()


def get_pr_diff(token: str, pr_number: int, max_chars: int = MAX_DIFF_CHARS) -> str:
    """Get the PR diff in unified diff format."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}"
    headers = gh_headers(token)
    headers["Accept"] = "application/vnd.github.diff"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    diff = resp.text
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n\n... [DIFF TRUNCATED - too large] ..."
    return diff


def get_pr_files(token: str, pr_number: int) -> list[dict]:
    """Get list of files changed in the PR."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/files"
    resp = requests.get(url, headers=gh_headers(token), params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def review_pr_with_claude(
    pr_info: dict,
    diff: str,
    files: list[dict],
    focus_areas: str | None = None,
    review_context: str | None = None,
) -> str:
    """Send PR info to Claude for review."""
    client = create_anthropic_client()

    files_summary = "\n".join(
        f"- `{f['filename']}` (+{f['additions']}/-{f['deletions']}, {f['status']})"
        for f in files
    )

    focus_section = ""
    if focus_areas:
        focus_section = f"\n## Specific Focus Areas\n{focus_areas}\n"

    context_section = ""
    if review_context:
        context_section = f"\n## Additional Context\n{review_context}\n"

    template = load_prompt_template("pr-review-api")
    if template:
        prompt = template.format(
            pr_title=pr_info['title'],
            pr_author=pr_info['user']['login'],
            pr_head_ref=pr_info['head']['ref'],
            pr_base_ref=pr_info['base']['ref'],
            pr_body=pr_info.get('body', 'No description provided.'),
            num_files=len(files),
            files_summary=files_summary,
            focus_section=focus_section,
            context_section=context_section,
            diff=diff,
        )
    else:
        prompt = (
            f"You are an expert code reviewer for sglang (LLM serving framework, "
            f"NVIDIA/AMD/NPU/XPU).\n\n"
            f"## PR: {pr_info['title']} by {pr_info['user']['login']}\n\n"
            f"## Files Changed ({len(files)} files)\n{files_summary}\n"
            f"{focus_section}{context_section}\n"
            f"## Diff\n```diff\n{diff}\n```\n\n"
            f"Provide a thorough code review: Summary, Code Quality, Performance, "
            f"Security, Testing, Suggestions."
        )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Agent mode: context
# ---------------------------------------------------------------------------

def pick_tier(files: list[dict], override: str = "") -> str:
    """``quick`` for small PRs, ``standard`` otherwise, unless overridden."""
    if override in TIERS:
        return override
    lines = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    return "quick" if lines <= QUICK_MAX_LINES and len(files) <= QUICK_MAX_FILES else "standard"


def angles_for(tier: str) -> tuple[str, ...]:
    return ("all",) if tier == "quick" else ANGLES


_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)


def _frontmatter(text: str) -> dict:
    """``name``, ``description`` and ``paths`` from a rule's or skill's front matter."""
    m = _FRONTMATTER_RE.match(text)
    out: dict = {"paths": []}
    if not m:
        return out
    key = None
    for row in m.group(1).splitlines():
        item = re.match(r"\s+-\s+(.*)", row)
        if item and key == "paths":
            out["paths"].append(item.group(1).strip().strip("\"'"))
            continue
        kv = re.match(r"([A-Za-z_]+):\s*(.*)", row)
        if kv:
            key = kv.group(1)
            if kv.group(2) and key != "paths":
                out[key] = kv.group(2).strip().strip("\"'")
    return out


def _glob_re(pattern: str) -> re.Pattern:
    """A repo-path glob (``**`` spans directories) as a regex."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def upstream_context(repo: Path, changed: list[str]) -> dict[str, str]:
    """The maintainers' rules and skills from *repo*, and an index of what each covers."""
    files: dict[str, str] = {}
    rule_rows, skill_rows = [], []
    for path in sorted((repo / ".claude" / "rules").glob("*.md")):
        text = path.read_text(errors="replace")
        globs = _frontmatter(text)["paths"]
        covered = [c for c in changed if any(_glob_re(g).match(c) for g in globs)] if globs else changed
        name = f"upstream/rules/{path.name}"
        files[name] = text
        scope = ", ".join(f"`{g}`" for g in globs) or "all files"
        hits = ", ".join(f"`{c}`" for c in covered[:10]) + (" …" if len(covered) > 10 else "")
        rule_rows.append(f"| `{name}` | {scope} | {hits or '—'} |")
    for path in sorted((repo / ".agents" / "skills").glob("*/SKILL.md")):
        text = path.read_text(errors="replace")
        meta = _frontmatter(text)
        name = f"upstream/skills/{path.parent.name}.md"
        files[name] = text
        desc = " ".join(meta.get("description", "").split()).replace("|", "\\|")
        skill_rows.append(f"| `{name}` | {desc} |")
    files["upstream/INDEX.md"] = "\n".join([
        "# sglang maintainers' rules and skills (from `main`)",
        "",
        "A rule applies to the changed files it covers. Read a skill when the change",
        "touches what it describes.",
        "",
        "## Rules",
        "",
        "| Rule | Covers | Changed files it covers |",
        "|---|---|---|",
        *(rule_rows or ["| — | — | — |"]),
        "",
        "## Skills",
        "",
        "| Skill | What it covers |",
        "|---|---|",
        *(skill_rows or ["| — | — |"]),
        "",
    ])
    return files


def render_pr_md(pr: dict, files: list[dict]) -> str:
    rows = [
        f"- `{f['filename']}` (+{f.get('additions', 0)}/-{f.get('deletions', 0)}, {f.get('status', '')})"
        for f in files
    ]
    return "\n".join([
        f"# PR #{pr['number']}: {pr.get('title', '')}",
        "",
        f"- Author: {(pr.get('user') or {}).get('login', '')}",
        f"- Base: `{(pr.get('base') or {}).get('ref', '')}` · head commit `{(pr.get('head') or {}).get('sha', '')}`",
        "",
        "## Description",
        "",
        (pr.get("body") or "(no description)").strip(),
        "",
        f"## Changed files ({len(files)})",
        "",
        *rows,
        "",
    ])


def get_pr_reviews(token: str, pr_number: int) -> list[dict]:
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/reviews"
    out, page = [], 1
    while True:
        resp = requests.get(url, headers=gh_headers(token), params={"per_page": 100, "page": page})
        resp.raise_for_status()
        batch = resp.json()
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def token_login(token: str) -> str:
    resp = requests.get("https://api.github.com/user", headers=gh_headers(token))
    return resp.json().get("login", "") if resp.ok else ""


def previous_rounds(token: str, pr_number: int) -> list[dict]:
    """Markers of this bot's earlier agent reviews of the PR, oldest first."""
    login = token_login(token)
    rounds = []
    for review in get_pr_reviews(token, pr_number):
        if login and (review.get("user") or {}).get("login") != login:
            continue
        meta = parse_marker(review.get("body") or "")
        if meta and meta.get("sha"):
            rounds.append(meta)
    return rounds


def render_previous(rounds: list[dict]) -> str:
    lines = [
        f"# Earlier amd-bot reviews of this PR: {len(rounds)}",
        "",
        f"Last reviewed commit: `{rounds[-1]['sha']}`. `since-last-review.diff` has the changes since.",
        "",
        "## Findings already posted (do not repeat them)",
        "",
    ]
    posted = [f for r in rounds for f in r.get("findings") or []]
    lines += [f"- `{f.get('file')}:{f.get('line')}` [{f.get('category')}] {f.get('title')}" for f in posted]
    if not posted:
        lines.append("(none)")
    return "\n".join(lines) + "\n"


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Agent mode: running the agents
# ---------------------------------------------------------------------------

@dataclass
class AgentCall:
    tag: str
    prompt: str
    context_files: dict[str, str]
    max_turns: int
    timeout_secs: int
    must_contain: str


def _pr_worktree(tag: str, head_sha: str, pr_number: int) -> Path:
    """A worktree at exactly the PR head; never silently at another commit."""
    wt = create_agent_worktree(tag, head_sha=head_sha)
    if _git(wt, "rev-parse", "HEAD").stdout.strip() != head_sha and not checkout_commit(
        wt, head_sha, fetch_ref=f"pull/{pr_number}/head",
    ):
        remove_agent_worktree(wt)
        raise RuntimeError(f"could not check out PR head {head_sha[:12]}")
    return wt


def run_agents(calls: list[AgentCall], head_sha: str, pr_number: int) -> list[str | Exception]:
    """Run *calls* in parallel, each in its own worktree at the PR head."""
    worktrees: dict[str, Path] = {}
    results: list[str | Exception] = []
    try:
        for call in calls:
            worktrees[call.tag] = _pr_worktree(call.tag, head_sha, pr_number)
        with ThreadPoolExecutor(max_workers=max(1, REVIEW_PARALLEL)) as pool:
            futures = [
                pool.submit(
                    claude_code_analyze,
                    prompt=call.prompt,
                    work_dir=worktrees[call.tag],
                    context_files=call.context_files,
                    max_turns=call.max_turns,
                    timeout_secs=call.timeout_secs,
                    output_must_contain=call.must_contain,
                )
                for call in calls
            ]
            for fut in futures:
                exc = fut.exception()
                results.append(exc if exc is not None else fut.result())
    finally:
        for wt in worktrees.values():
            try:
                remove_agent_worktree(wt)
            except Exception as exc:
                print(f"  WARNING: could not remove worktree {wt}: {exc}")
    return results


# ---------------------------------------------------------------------------
# Agent mode: the review
# ---------------------------------------------------------------------------

@dataclass
class AgentReview:
    pr_number: int
    head_sha: str
    tier: str
    angles: tuple[str, ...]
    round: int
    findings: list[Finding] = field(default_factory=list)   # confirmed
    candidates: int = 0          # findings sent to the verifiers
    dropped_by_votes: int = 0
    failed_reviewers: list[str] = field(default_factory=list)
    voters: int = 0
    previous: list[dict] = field(default_factory=list)


def agent_review(
    token: str,
    pr_number: int,
    focus_areas: str | None = None,
    review_context: str | None = None,
    tier_override: str = "",
) -> AgentReview:
    """Find, check, verify. Raises if no reviewer or too few verifiers finished."""
    pr = get_pr_info(token, pr_number)
    head_sha = pr["head"]["sha"]
    files = get_pr_changed_files(token, pr_number)
    changed = [f["filename"] for f in files]
    tier = pick_tier(files, tier_override)
    angles = angles_for(tier)

    repo = ensure_sglang_repo()
    _git(repo, "fetch", "origin", f"pull/{pr_number}/head", "--depth", "100")

    diff = get_pr_diff(token, pr_number, max_chars=MAX_AGENT_DIFF_CHARS)
    rounds = previous_rounds(token, pr_number)
    review = AgentReview(pr_number, head_sha, tier, angles, len(rounds) + 1, previous=rounds)
    print(f"  Tier {tier} ({len(files)} files), reviewers: {', '.join(angles)}, round {review.round}")

    context = {
        "pr.md": render_pr_md(pr, files),
        "pr.diff": diff,
        "ci-runtime-env.md": collect_ci_runtime_env(repo),
        **upstream_context(repo, changed),
    }
    if rounds:
        context["previous-review.md"] = render_previous(rounds)
        since = _git(repo, "diff", rounds[-1]["sha"], head_sha)
        context["since-last-review.diff"] = (
            since.stdout if since.returncode == 0
            else "(the last reviewed commit is no longer available, e.g. after a force-push; review pr.diff)\n"
        )

    base_prompt = [
        f"PR: #{pr_number}",
        f"Repo: {REPO}",
        f"Head commit: {head_sha}",
        "Source: current directory (the PR head)",
        "GitHub API token: $GH_PAT",
    ]
    if focus_areas:
        base_prompt.append(f"Focus: {focus_areas}")
    if review_context:
        base_prompt.append(f"Additional context: {review_context}")

    # 1. Find
    find_calls = [
        AgentCall(
            tag=f"review-pr{pr_number}-{angle}",
            prompt="\n".join(["Task: PR Code Review", *base_prompt,
                              f"Angle: {angle}", f"Tier: {tier}", f"Round: {review.round}"]),
            context_files=context,
            max_turns=FIND_MAX_TURNS,
            timeout_secs=FIND_TIMEOUT_SECS,
            must_contain="<!-- review-findings",
        )
        for angle in angles
    ]
    found: list[Finding] = []
    errors: list[str] = []
    for angle, result in zip(angles, run_agents(find_calls, head_sha, pr_number)):
        try:
            if isinstance(result, Exception):
                raise result
            found += parse_findings(result, angle)
        except Exception as exc:
            print(f"  Reviewer '{angle}' failed: {exc}")
            review.failed_reviewers.append(angle)
            errors.append(f"{angle}: {exc}")
    if len(review.failed_reviewers) == len(angles):
        raise RuntimeError("no reviewer finished: " + "; ".join(errors))

    # 2. Check, merge, drop repeats, cap
    upstream_files = {name for name in context if name.startswith("upstream/")}
    kept, dropped = check_findings(found, set(changed), diff_hunk_lines(diff), upstream_files)
    kept = merge_findings(kept)
    kept, repeats = drop_repeats(kept, [f for r in rounds for f in r.get("findings") or []])
    kept, capped = cap_findings(kept, REVIEW_MAX_FINDINGS)
    for f, reason in dropped + repeats + capped:
        print(f"  Dropped {f.file}:{f.line} '{f.title}': {reason}")
    review.candidates = len(kept)
    if not kept:
        return review

    # 3. Verify
    candidates = [{"id": f.id, **fingerprint(f), "severity": f.severity,
                   "pre_existing": f.pre_existing, "explanation": f.explanation,
                   "evidence": f.evidence, "suggestion": f.suggestion, "rule": f.rule,
                   "other_locations": f.other_locations} for f in kept]
    verify_context = {
        "candidates.json": json.dumps({"candidates": candidates}, indent=2),
        "pr.md": context["pr.md"],
        "pr.diff": diff,
        "ci-runtime-env.md": context["ci-runtime-env.md"],
        **{k: v for k, v in context.items() if k.startswith("upstream/")},
    }
    verify_calls = [
        AgentCall(
            tag=f"review-pr{pr_number}-verify{i}",
            prompt="\n".join(["Task: PR Code Review Verification", *base_prompt,
                              f"Verifier: {i} of {REVIEW_VOTERS}"]),
            context_files=verify_context,
            max_turns=VERIFY_MAX_TURNS,
            timeout_secs=VERIFY_TIMEOUT_SECS,
            must_contain="<!-- review-votes",
        )
        for i in range(1, REVIEW_VOTERS + 1)
    ]
    ballots = []
    for i, result in enumerate(run_agents(verify_calls, head_sha, pr_number), 1):
        try:
            if isinstance(result, Exception):
                raise result
            ballots.append(parse_votes(result))
        except Exception as exc:
            print(f"  Verifier {i} failed: {exc}")
    review.voters = len(ballots)
    confirmed, rejected = tally(kept, ballots, REVIEW_VOTES_REQUIRED)
    for f, reason in rejected:
        print(f"  Not posted {f.file}:{f.line} '{f.title}': {reason}")
    review.findings = confirmed
    review.dropped_by_votes = sum(not reason.startswith(DUPLICATE_REASON) for _, reason in rejected)
    return review


def render_review(review: AgentReview, requester_line: str = "") -> tuple[str, list[dict]]:
    """The review summary and its inline comments (``path`` / ``line`` / ``body``)."""
    sha = review.head_sha
    inline = [f for f in review.findings if f.inline]
    listed = [f for f in review.findings if not f.inline]
    comments = [{"path": f.file, "line": f.line, "side": "RIGHT", "body": render_inline(f)} for f in inline]

    reviewers = len(review.angles) - len(review.failed_reviewers)
    meta = (f"Reviewed [`{sha[:8]}`](https://github.com/{REPO}/commit/{sha}) · "
            f"{review.tier} tier: {reviewers} reviewer{'s' if reviewers != 1 else ''}")
    if review.candidates:
        meta += f", {review.voters} verifiers ({REVIEW_VOTES_REQUIRED} must confirm a finding)"
    meta += f" · round {review.round}"
    lines = [f"{requester_line}## amd-bot review", "", count_line(review.findings), "", f"<sub>{meta}</sub>"]

    if review.round > 1:
        last = review.previous[-1]["sha"]
        lines += ["", f"Changes since the last review ([`{last[:8]}`](https://github.com/{REPO}/commit/{last})) "
                      "were reviewed; findings already posted are not repeated."]
    if review.round > MAX_ROUNDS:
        lines += ["", "> [!NOTE]", f"> This PR has had {review.round} review rounds. If findings keep coming, "
                      "consider stepping back to reconsider the approach rather than fixing them one at a time."]
    if review.failed_reviewers:
        lines += ["", "> [!WARNING]", "> These reviewers did not finish, so their angle was not covered: "
                  + ", ".join(f"`{a}`" for a in review.failed_reviewers) + ". Re-run `@amd-bot review` to retry."]
    if inline:
        lines += ["", f"{len(inline)} finding{'s are' if len(inline) != 1 else ' is'} commented inline."]
    for title, group in (
        ("### Findings not on a changed line", [f for f in listed if not f.pre_existing]),
        ("### Pre-existing (not introduced by this PR)", [f for f in listed if f.pre_existing]),
    ):
        if group:
            lines += ["", title]
            lines += [part for f in group for part in ("", render_listed(f, REPO, sha))]
    if review.dropped_by_votes:
        n = review.dropped_by_votes
        lines += ["", f"<sub>{n} candidate finding{'s were' if n != 1 else ' was'} not confirmed by the "
                      f"verifiers and {'are' if n != 1 else 'is'} not shown. Style and cleanup are not "
                      f"reported; at most {MAX_NITS} nits are.</sub>"]

    marker = render_marker({
        "v": 2, "sha": sha, "round": review.round, "tier": review.tier,
        "findings": [fingerprint(f) for f in review.findings],
    })
    lines += ["", marker, "", "---", "*Generated by amd-bot using Claude Code CLI*", ""]
    return "\n".join(lines), comments


def publish_review(token: str, pr_number: int, head_sha: str, body: str, comments: list[dict]) -> str:
    """Post one COMMENT review with inline comments; fold them into the body if GitHub refuses."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/reviews"
    payload = {"commit_id": head_sha, "event": "COMMENT", "body": body, "comments": comments}
    resp = requests.post(url, headers=gh_headers(token), json=payload)
    if resp.status_code == 422 and comments:
        print(f"  Inline comments refused ({resp.text[:200]}); posting them in the review body")
        folded = body.replace(
            "\n---\n*Generated by",
            "\n### Findings\n\n" + "\n\n".join(
                f"#### `{c['path']}:{c['line']}`\n\n{c['body']}" for c in comments
            ) + "\n\n---\n*Generated by",
            1,
        )
        resp = requests.post(url, headers=gh_headers(token),
                             json={"commit_id": head_sha, "event": "COMMENT", "body": folded})
    resp.raise_for_status()
    return resp.json().get("html_url", "")


def preview(body: str, comments: list[dict]) -> str:
    parts = [body]
    for c in comments:
        parts.append(f"--- inline: {c['path']}:{c['line']} ---\n{c['body']}")
    return "\n\n".join(parts)


def review_pr(
    token: str,
    pr_number: int,
    focus_areas: str | None = None,
    review_context: str | None = None,
    post_comment_flag: bool = True,
    use_agent: bool = True,
    tier: str = "",
) -> str:
    """Main function to review a PR."""
    comment_author = os.environ.get("COMMENT_AUTHOR", "")
    requester_line = f"> @{comment_author} requested a review\n\n" if comment_author else ""

    if use_agent:
        try:
            if not claude_code_available():
                raise RuntimeError("Claude Code CLI not found on the runner")
            print(f"Reviewing PR #{pr_number} (agent mode)...")
            review = agent_review(token, pr_number, focus_areas, review_context, tier)
        except Exception as exc:
            # No API fallback: tell the requester the agent did not work.
            print(f"  ERROR: Agent review failed ({exc}); not falling back to the API")
            now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            body = (f"{requester_line}## amd-bot review\n\n> PR #{pr_number} — {now}\n\n"
                    f"{agent_failure_notice('the review', exc)}\n\n---\n*Generated by amd-bot*\n")
            if post_comment_flag:
                post_comment(token, REPO, pr_number, body)
            else:
                print(body)
            raise
        body, comments = render_review(review, requester_line)
        if not post_comment_flag:
            out = preview(body, comments)
            print(out)
            return out
        url = publish_review(token, pr_number, review.head_sha, body, comments)
        print(f"  Posted review: {url}")
        return url

    # --- API mode (USE_AGENT=false) ---
    print(f"Reviewing PR #{pr_number}...")

    pr_info = get_pr_info(token, pr_number)
    print(f"  Title: {pr_info['title']}")
    print(f"  Author: {pr_info['user']['login']}")

    diff = get_pr_diff(token, pr_number)
    print(f"  Diff size: {len(diff)} chars")

    files = get_pr_files(token, pr_number)
    print(f"  Files changed: {len(files)}")

    print("  Sending to Claude for review...")
    review = review_pr_with_claude(pr_info, diff, files, focus_areas, review_context)

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    body = f"""{requester_line}## Claude Code Review

> PR #{pr_number}: {pr_info['title']}
> Reviewed at {now}

{review}

---
*Generated by amd-bot using Claude API*
"""

    if post_comment_flag:
        result = post_comment(token, REPO, pr_number, body)
        print(f"  Posted review: {result['html_url']}")
        return result["html_url"]
    else:
        print(body)
        return body


def main():
    parser = argparse.ArgumentParser(description="Review a sglang PR with Claude")
    parser.add_argument("pr_number", type=int, help="PR number to review")
    parser.add_argument(
        "--focus",
        help="Specific areas to focus on (e.g., 'AMD ROCm compatibility, memory management')",
    )
    parser.add_argument(
        "--context",
        help="Additional context for the review",
    )
    parser.add_argument(
        "--tier", choices=("auto", *TIERS),
        default=os.environ.get("REVIEW_TIER") or "auto",
        help="Agent review tier (default: auto, from the PR's size; env REVIEW_TIER)",
    )
    parser.add_argument(
        "--no-post",
        action="store_true",
        help="Print review to stdout instead of posting",
    )
    parser.add_argument(
        "--use-agent", action=argparse.BooleanOptionalAction,
        default=os.environ.get("USE_AGENT", "").lower() not in ("false", "0", "no"),
        help="Use Claude Code agent (default: enabled, use --no-use-agent to disable)",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")),
    )

    args = parser.parse_args()

    if not args.github_token:
        print("Error: GitHub token required. Set GH_PAT.", file=sys.stderr)
        sys.exit(1)

    if not args.use_agent:
        if not os.environ.get("LLM_GATEWAY_KEY"):
            print("Error: LLM_GATEWAY_KEY env var required.", file=sys.stderr)
            sys.exit(1)
        if not os.environ.get("LLM_GATEWAY_URL"):
            print("Error: LLM_GATEWAY_URL env var required.", file=sys.stderr)
            sys.exit(1)

    review_pr(
        args.github_token,
        args.pr_number,
        focus_areas=args.focus,
        review_context=args.context,
        post_comment_flag=not args.no_post,
        use_agent=args.use_agent,
        tier="" if args.tier == "auto" else args.tier,
    )


if __name__ == "__main__":
    main()
