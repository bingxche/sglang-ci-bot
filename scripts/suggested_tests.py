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

from utils import get_file_content_at_ref

# .ci-context/sglang-skills/<name> <- path on sglang main
SKILL_FILES = {
    "rerun-test.md": ".agents/skills/rerun-test/SKILL.md",
    "write-sglang-test.md": ".agents/skills/write-sglang-test/SKILL.md",
}
DISCLAIMER = "🤖 AI-generated suggestion — use it with your own judgement."
# Platform key -> (name, the comment that runs its extra tier; contribution guide "How to trigger CI tests")
PLATFORMS = {"nv": ("NVIDIA", "/run-extra-ci nv"), "amd": ("AMD", "/run-extra-ci amd")}
_BLOCK_RE = re.compile(r"<!--\s*test-selection\s*(\{.*?\})\s*-->", re.DOTALL)


def main_context(token: str, fetch=None) -> dict[str, str]:
    """The skill files from sglang ``main`` as context files; one that cannot be fetched is left out."""
    fetch = fetch or get_file_content_at_ref

    def get(path: str) -> str | None:
        try:
            return fetch(token, path, "main")
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
    return _BLOCK_RE.sub("", report).rstrip() + "\n"


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


def neutralize_outside_code(text: str, neutralize) -> str:
    """Apply *neutralize* (``neutralize_mentions``) to everything except fenced code blocks.

    GitHub pings nobody from inside a fence, and rewriting ``@decorator`` lines there garbles code.
    """
    parts = re.split(r"(^```.*?^```[^\n]*$)", text, flags=re.M | re.S)
    return "".join(p if i % 2 else neutralize(p) for i, p in enumerate(parts))
