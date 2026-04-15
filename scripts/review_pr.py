#!/usr/bin/env python3
"""
amd-bot PR Review for sglang.

Fetches PR diff and file changes, sends to Claude for review,
and posts the review as a PR comment.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import requests

from utils import (
    CLAUDE_MODEL,
    REPO,
    agent_worktree,
    claude_code_analyze,
    claude_code_available,
    create_anthropic_client,
    gh_headers,
    load_prompt_template,
    post_comment,
)

MAX_DIFF_CHARS = 120000


def get_pr_info(token: str, pr_number: int) -> dict:
    """Get PR metadata."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}"
    resp = requests.get(url, headers=gh_headers(token))
    resp.raise_for_status()
    return resp.json()


def get_pr_diff(token: str, pr_number: int) -> str:
    """Get the PR diff in unified diff format."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}"
    headers = gh_headers(token)
    headers["Accept"] = "application/vnd.github.diff"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    diff = resp.text
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n... [DIFF TRUNCATED - too large] ..."
    return diff


def get_pr_files(token: str, pr_number: int) -> list[dict]:
    """Get list of files changed in the PR."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/files"
    resp = requests.get(url, headers=gh_headers(token), params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def get_pr_comments(token: str, pr_number: int) -> list[dict]:
    """Get existing review comments on the PR."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/comments"
    resp = requests.get(url, headers=gh_headers(token), params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def get_pr_review_comments(token: str, pr_number: int) -> list[dict]:
    """Get issue-level comments on the PR."""
    url = f"https://api.github.com/repos/{REPO}/issues/{pr_number}/comments"
    resp = requests.get(url, headers=gh_headers(token), params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def get_file_content(token: str, path: str, ref: str) -> str | None:
    """Get file content at a specific ref."""
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    headers = gh_headers(token)
    headers["Accept"] = "application/vnd.github.raw"
    resp = requests.get(url, headers=headers, params={"ref": ref})
    if resp.status_code == 200:
        return resp.text
    return None


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
            f"Security, Testing, Suggestions, Overall Assessment."
        )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def review_pr_with_agent(
    pr_number: int,
    repo_path,
    focus_areas: str | None = None,
    review_context: str | None = None,
) -> str:
    """Use Claude Code agent for deep PR review.

    The agent autonomously fetches the PR diff, reads full source files,
    finds callers of modified functions, checks test coverage, and
    explores related code.
    """
    lines = [
        f"Task: PR Code Review",
        f"PR: #{pr_number}",
        f"Repo: sgl-project/sglang",
        f"Source: current directory (checked out to PR branch)",
        f"GitHub API token: $GH_PAT",
    ]
    if focus_areas:
        lines.append(f"Focus areas: {focus_areas}")
    if review_context:
        lines.append(f"Additional context: {review_context}")
    prompt = "\n".join(lines)

    return claude_code_analyze(
        prompt=prompt,
        work_dir=repo_path,
        max_turns=1000,
        timeout_secs=600,
    )


def review_pr(
    token: str,
    pr_number: int,
    focus_areas: str | None = None,
    review_context: str | None = None,
    post_comment_flag: bool = True,
    use_agent: bool = False,
) -> str:
    """Main function to review a PR."""
    comment_author = os.environ.get("COMMENT_AUTHOR", "")
    requester_line = f"> @{comment_author} requested a review\n\n" if comment_author else ""

    if use_agent:
        if not claude_code_available():
            print("  WARNING: --use-agent but Claude Code not found, falling back to API")
            use_agent = False
        else:
            try:
                print(f"Reviewing PR #{pr_number} (agent mode, worktree)...")
                with agent_worktree(f"review-pr{pr_number}", pr_number=pr_number) as wt_path:
                    review = review_pr_with_agent(
                        pr_number, wt_path, focus_areas, review_context,
                    )
                    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                    body = f"""{requester_line}## Claude Code Review

> PR #{pr_number} — Reviewed at {now}

{review}

---
*Generated by amd-bot using Claude Code CLI*
"""
                    if post_comment_flag:
                        result = post_comment(token, REPO, pr_number, body)
                        print(f"  Posted review: {result['html_url']}")
                        return result["html_url"]
                    print(body)
                    return body
            except Exception as exc:
                print(f"  WARNING: Agent failed ({exc}), falling back to API")
                use_agent = False

    # --- Non-agent (API) path ---
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
        "--no-post",
        action="store_true",
        help="Print review to stdout instead of posting",
    )
    parser.add_argument(
        "--use-agent", action="store_true",
        default=os.environ.get("USE_AGENT", "").lower() in ("true", "1", "yes"),
        help="Use Claude Code agent for deeper review (reads full source files)",
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
    )


if __name__ == "__main__":
    main()
