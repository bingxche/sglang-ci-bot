"""Whether a PR needs extra CI, in the ci-status report.

The ci-status agent decides, per platform, whether the PR should run the
extra tier, with sglang main's own rules: the ``write-sglang-test`` and
``rerun-test`` skills, copied from ``main`` into ``.ci-context/sglang-skills/``
(never read from the PR checkout, which the PR can change). It ends its report
with a hidden ``test-selection`` block; code renders the section from it. The
bot only suggests; it triggers nothing (R3).
"""

from __future__ import annotations

import json
import re

import requests

from utils import REPO, get_file_content_at_ref, gh_get, gh_headers

# .ci-context/sglang-skills/<name> <- path on sglang main
SKILL_FILES = {
    "rerun-test.md": ".agents/skills/rerun-test/SKILL.md",
    "write-sglang-test.md": ".agents/skills/write-sglang-test/SKILL.md",
}
DISCLAIMER = "🤖 AI-generated suggestion — use it with your own judgement."
# Platform key -> (name, the comment that runs its extra tier; contribution guide "How to trigger CI tests")
PLATFORMS = {"nv": ("NVIDIA", "/run-extra-ci nv"), "amd": ("AMD", "/run-extra-ci amd")}
_MARKER = "<!-- test-selection"
_BLOCK_RE = re.compile(r"<!--\s*test-selection\s*(\{.*?\})\s*-->", re.DOTALL)
# CommonMark fences: up to 3 spaces of indent; a backtick fence's info string has no backtick.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}(?=[^`]*$)|~{3,})")
# Spans GitHub never turns into a mention: inline code and URLs.
_INLINE_SAFE_RE = re.compile(r"(`+)[^\n]*?\1|https?://[^\s)>\]]+")


def _main_at(token: str, as_of: str | None) -> str:
    """``main``, or for a replay the ``main`` commit current at *as_of* (later skills may know the answer)."""
    if not as_of:
        return "main"
    try:
        resp = gh_get(f"https://api.github.com/repos/{REPO}/commits", headers=gh_headers(token),
                      params={"sha": "main", "until": as_of, "per_page": 1})
        if resp.status_code == 200 and resp.json():
            return resp.json()[0]["sha"]
    except (requests.RequestException, ValueError, KeyError, IndexError):
        pass
    return "main"


def main_context(token: str, as_of: str | None = None, fetch=None) -> dict[str, str]:
    """The skill files from sglang ``main`` as context files; one that cannot be fetched is left out."""
    fetch = fetch or get_file_content_at_ref
    ref = _main_at(token, as_of)

    def get(path: str) -> str | None:
        try:
            return fetch(token, path, ref)
        except requests.RequestException:
            return None

    return {f"sglang-skills/{name}": text for name, path in SKILL_FILES.items() if (text := get(path))}


def parse_block(report: str) -> dict | None:
    matches = _BLOCK_RE.findall(report)
    if not matches:
        return None
    try:
        block = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    return block if isinstance(block, dict) else None


def strip_block(report: str) -> str:
    """The report without its block, which is its last part.

    Cut from the last marker (parse_block reads the last block; prose may quote the
    marker earlier) rather than removing a regex match: a "} -->" inside the block's
    text would end the match early and leave the rest of the JSON visible.
    """
    cut = report.rfind(_MARKER)
    if cut < 0:
        return report.rstrip() + "\n"
    lines = report[:cut].rstrip().split("\n")
    # A block the agent wrapped in a fence (as the task file shows it) would leave an open fence.
    if _FENCE_OPEN_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip() + "\n"


def _text(value, limit: int = 600) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + "…" if len(text) > limit else text


def check(block: dict | None) -> dict:
    """Per platform: ``extra_needed`` (True / False / None when the agent did not say) and the reason."""
    platforms = {}
    for key in PLATFORMS:
        raw = block.get(key) if isinstance(block, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        extra = raw.get("extra_needed")
        platforms[key] = {"extra_needed": extra if isinstance(extra, bool) else None,
                          "reason": _text(raw.get("reason"))}
    return {"valid": block is not None, "platforms": platforms}


def render(result: dict) -> str:
    """The "Suggested extra CI" section, from the checked block."""
    lines = ["### Suggested extra CI", "", f"> {DISCLAIMER}", ""]
    if not result["valid"]:
        lines += ["The agent gave no readable suggestion this time.", ""]
    else:
        for key, (name, trigger) in PLATFORMS.items():
            sel = result["platforms"][key]
            need = {True: "yes", False: "no", None: "not stated"}[sel["extra_needed"]]
            lines.append(f"- **{name}**: extra CI needed: **{need}**. {sel['reason']}".rstrip())
            if sel["extra_needed"]:
                lines.append(f"  - To run it, comment `{trigger}`.")
        lines.append("")
    lines.append(f"> {DISCLAIMER}")
    return "\n".join(lines) + "\n"


def _neutralize_inline(text: str, neutralize) -> str:
    out, last = [], 0
    for m in _INLINE_SAFE_RE.finditer(text):
        out += [neutralize(text[last:m.start()]), m.group(0)]
        last = m.end()
    return "".join(out + [neutralize(text[last:])])


def neutralize_outside_code(text: str, neutralize) -> str:
    """Apply *neutralize* (``neutralize_mentions``) except where GitHub never pings: fenced code
    blocks as CommonMark reads them (an unclosed fence runs to the end), inline code and URLs.

    Rewriting ``@decorator`` lines in code, or ``/@user`` in a URL, would garble them.
    """
    out, plain, fence = [], [], None
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        if fence:
            out.append(line)
            if re.match(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", body):
                fence = None
            continue
        m = _FENCE_OPEN_RE.match(body)
        if m:
            out.append(_neutralize_inline("".join(plain), neutralize))
            plain, fence = [], m.group(1)
            out.append(line)
        else:
            plain.append(line)
    out.append(_neutralize_inline("".join(plain), neutralize))
    return "".join(out)
