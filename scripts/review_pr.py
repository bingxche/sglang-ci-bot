#!/usr/bin/env python3
"""
PR Review Bot for sglang.

Fetches PR diff and file changes, sends to Claude for review,
and posts the review as a PR comment.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import anthropic
import httpx
import requests

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO = f"{REPO_OWNER}/{REPO_NAME}"
CLAUDE_MODEL = "claude-opus-4-6"


def _create_anthropic_client(api_key: str) -> anthropic.Anthropic:
    """Create Anthropic client, supporting AMD LLM Gateway (Azure APIM) or direct API."""
    gateway_key = os.environ.get("LLM_GATEWAY_KEY") or os.environ.get("APIM_SUBSCRIPTION_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")

    if gateway_key and base_url:
        import getpass
        headers = {
            "Ocp-Apim-Subscription-Key": gateway_key,
            "user": getpass.getuser(),
            "anthropic-version": "vertex-2023-10-16",
        }
        return anthropic.Anthropic(
            base_url=base_url,
            api_key="dummy",
            http_client=httpx.Client(verify=False),
            default_headers=headers,
        )

    return anthropic.Anthropic(api_key=api_key)
MAX_DIFF_CHARS = 120000


def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


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
    api_key: str,
    pr_info: dict,
    diff: str,
    files: list[dict],
    focus_areas: str | None = None,
    review_context: str | None = None,
) -> str:
    """Send PR info to Claude for review."""
    client = _create_anthropic_client(api_key)

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

    prompt = f"""You are an expert code reviewer for sglang, a fast serving framework for large language models. 
The project supports NVIDIA, AMD (ROCm), NPU, and XPU backends.

Review the following Pull Request carefully.

## PR Information
- **Title**: {pr_info['title']}
- **Author**: {pr_info['user']['login']}
- **Branch**: {pr_info['head']['ref']} -> {pr_info['base']['ref']}
- **Description**: 
{pr_info.get('body', 'No description provided.')}

## Files Changed ({len(files)} files)
{files_summary}
{focus_section}{context_section}
## Diff
```diff
{diff}
```

Please provide a thorough code review covering:

1. **Summary**: What does this PR do? (2-3 sentences)
2. **Code Quality**: 
   - Any bugs, logic errors, or edge cases?
   - Code style and readability
   - Error handling
3. **Performance**: Any performance concerns? Especially for serving/inference workloads.
4. **Security**: Any security issues?
5. **Testing**: Are the changes adequately tested? What tests should be added?
6. **Suggestions**: Specific, actionable improvement suggestions with code examples where helpful.
7. **Overall Assessment**: Approve / Request Changes / Comment, with reasoning.

Format as clear Markdown. Be constructive and specific. Reference file names and line numbers when possible."""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def post_pr_review_comment(token: str, pr_number: int, body: str) -> dict:
    """Post a comment on the PR."""
    url = f"https://api.github.com/repos/{REPO}/issues/{pr_number}/comments"
    resp = requests.post(url, headers=gh_headers(token), json={"body": body})
    resp.raise_for_status()
    return resp.json()


def review_pr(
    token: str,
    anthropic_key: str,
    pr_number: int,
    focus_areas: str | None = None,
    review_context: str | None = None,
    post_comment: bool = True,
) -> str:
    """Main function to review a PR."""
    print(f"Reviewing PR #{pr_number}...")

    pr_info = get_pr_info(token, pr_number)
    print(f"  Title: {pr_info['title']}")
    print(f"  Author: {pr_info['user']['login']}")

    diff = get_pr_diff(token, pr_number)
    print(f"  Diff size: {len(diff)} chars")

    files = get_pr_files(token, pr_number)
    print(f"  Files changed: {len(files)}")

    print("  Sending to Claude for review...")
    review = review_pr_with_claude(
        anthropic_key, pr_info, diff, files, focus_areas, review_context
    )

    body = f"""## 🤖 Claude Code Review

> PR #{pr_number}: {pr_info['title']}
> Requested review at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

{review}

---
*Automated review by sglang-ci-bot using Claude. This is an AI-generated review — please use your judgment.*
"""

    if post_comment:
        comment = post_pr_review_comment(token, pr_number, body)
        print(f"  Posted review: {comment['html_url']}")
        return comment["html_url"]
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
        "--github-token",
        default=os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")),
    )
    parser.add_argument(
        "--anthropic-key",
        default=os.environ.get("ANTHROPIC_API_KEY", ""),
    )

    args = parser.parse_args()

    if not args.github_token:
        print("Error: GitHub token required.", file=sys.stderr)
        sys.exit(1)
    if not args.anthropic_key:
        print("Error: Anthropic API key required.", file=sys.stderr)
        sys.exit(1)

    review_pr(
        args.github_token,
        args.anthropic_key,
        args.pr_number,
        focus_areas=args.focus,
        review_context=args.context,
        post_comment=not args.no_post,
    )


if __name__ == "__main__":
    main()
