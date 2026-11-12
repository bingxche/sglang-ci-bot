"""Suggested tests in the ci-status report: sglang's own selection method plus a code gate.

The ci-status agent picks the tests a PR should run, following sglang main's
``rerun-test`` and ``write-sglang-test`` skills (copied from ``main`` into
``.ci-context/sglang-skills/``, never read from the PR checkout, which the PR
can change). It ends its report with a hidden ``test-selection`` block. Code
keeps only the files that exist in the checkout CI tested, are registered for
the platform and are not disabled, then writes the commands, so the section
never asks anyone to run a test that cannot run. The bot only suggests; it
triggers nothing (R3).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import requests

from utils import get_file_content_at_ref

# .ci-context/sglang-skills/<name> <- path on sglang main
SKILL_FILES = {
    "rerun-test.md": ".agents/skills/rerun-test/SKILL.md",
    "write-sglang-test.md": ".agents/skills/write-sglang-test/SKILL.md",
    "rerun_test_groups.json": "scripts/ci/rerun_test_groups.json",
}
# Its AMD_FLAG = "--amd" (sgl-project/sglang#48190) means /rerun-test can run AMD suites.
HANDLER_PATH = "scripts/ci/utils/slash_command_handler.py"
MAX_FILES = 8
# More extra-tier files than this on one platform: suggest the whole extra tier.
MAX_EXTRA_FILES = 3
# /rerun-test rejects a file estimated above 60 minutes (rerun-test skill, "Limits").
MAX_RERUN_SECS = 3600
DISCLAIMER = "🤖 AI-generated suggestion — use it with your own judgement."
PLATFORMS = {"nv": ("NVIDIA", "cuda"), "amd": ("AMD", "amd")}
_TIERS = ("base", "extra", "nightly", "weekly", "other")
_REGISTER_RE = re.compile(r"^register_(\w+)_ci$")
_BLOCK_RE = re.compile(r"<!--\s*test-selection\s*(\{.*?\})\s*-->", re.DOTALL)
_PATH_RE = re.compile(r"^[\w./-]+\.py$")
_SPEC_SUFFIX_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$")
_GROUP_RE = re.compile(r"^[\w.-]+$")
_CI_STATES = ("passed", "failed", "not_run", "pending")
# ci_register's positional order: register_*_ci(est_time, suite, nightly, disabled)
_POSITIONAL = ("est_time", "suite", "nightly", "disabled")


def main_context(token: str, fetch=None) -> tuple[dict[str, str], bool]:
    """The skill files from sglang ``main`` as context files, and whether ``/rerun-test --amd`` exists there.

    A file that cannot be fetched is left out; the agent is told which ones it has.
    """
    fetch = fetch or get_file_content_at_ref

    def get(path: str) -> str | None:
        try:
            return fetch(token, path, "main")
        except requests.RequestException:
            return None

    files = {f"sglang-skills/{name}": text for name, path in SKILL_FILES.items() if (text := get(path))}
    return files, bool(re.search(r"""=\s*["']--amd["']""", get(HANDLER_PATH) or ""))


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


def tier(suite: str, nightly: bool = False) -> str:
    """base / extra / nightly / weekly / other, from the suite name (CUDA and AMD names)."""
    if nightly or suite.startswith("nightly"):
        return "nightly"
    if suite.startswith("weekly"):
        return "weekly"
    if suite.startswith("extra"):
        return "extra"
    if suite.startswith(("base-", "stage-", "jit-kernel", "sgl-kernel")):
        return "base"
    return "other"


def registrations(text: str) -> list[dict]:
    """Module-level ``register_<backend>_ci(...)`` calls, read the way ``test/run_suite.py`` reads them.

    ``disabled`` counts when it is given at all and not ``None`` (as in run_suite.py).
    ``rerunnable`` is False for a CUDA registration without ``stage`` + ``runner_config``:
    ``/rerun-test`` cannot dispatch a legacy ``suite=`` string.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for stmt in tree.body:
        call = stmt.value if isinstance(stmt, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
            continue
        m = _REGISTER_RE.match(call.func.id)
        if not m:
            continue
        nodes = dict(zip(_POSITIONAL, call.args))
        nodes.update({k.arg: k.value for k in call.keywords if k.arg})
        kw = {k: v.value for k, v in nodes.items() if isinstance(v, ast.Constant)}
        staged = bool(kw.get("stage") and kw.get("runner_config"))
        suite = f"{kw['stage']}-test-{kw['runner_config']}" if staged else str(kw.get("suite") or "")
        disabled = "disabled" in nodes and not (isinstance(nodes["disabled"], ast.Constant)
                                                and nodes["disabled"].value is None)
        est = kw.get("est_time")
        out.append({"backend": m.group(1), "suite": suite, "tier": tier(suite, kw.get("nightly") is True),
                    "disabled": disabled, "est_time": est if isinstance(est, (int, float)) else None,
                    "rerunnable": staged or m.group(1) != "cuda"})
    return out


def _normalize(entry: str) -> tuple[str, str] | None:
    """``(file path from the repo root, spec for /rerun-test)``, or None when it is not a plain test path."""
    path, sep, rest = entry.strip().strip("`").partition("::")
    path = path.removeprefix("./")
    if path.startswith("registered/"):
        path = "test/" + path
    if not _PATH_RE.match(path) or ".." in path.split("/") or (sep and not _SPEC_SUFFIX_RE.match(rest)):
        return None
    return path, path.removeprefix("test/") + (f"::{rest}" if sep else "")


def _cell(value, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return (text[:limit] + "…" if len(text) > limit else text).replace("|", "\\|")


def _items(value) -> list:
    return value if isinstance(value, list) else []


def check(block: dict | None, repo_path: Path, groups: set[str], amd_rerun: bool) -> dict:
    """Keep the suggested files that exist, are registered for the platform and are not disabled.

    Files are read from the checkout CI tested, so a PR's new tests count and a
    test it deletes does not. At most ``MAX_FILES`` per platform are kept.
    Returns, per platform, what is kept, what was dropped and why, and the commands.
    """
    result = {"amd_rerun": amd_rerun, "valid": block is not None, "platforms": {}}
    for key, (_, backend) in PLATFORMS.items():
        raw = block.get(key) if isinstance(block, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        kept, dropped, by_path = [], [], {}
        for item in _items(raw.get("files")):
            item = item if isinstance(item, dict) else {"file": item}
            entry = str(item.get("file") or "")
            norm = _normalize(entry)
            if norm is None:
                dropped.append({"file": entry[:200], "reason": "not a test file path"})
                continue
            path, spec = norm
            if path in by_path:
                by_path[path]["spec"] = path.removeprefix("test/")  # two specs of one file: run all of it
                continue
            if len(kept) >= MAX_FILES:
                dropped.append({"file": path, "reason": f"over the limit of {MAX_FILES} files"})
                continue
            local = repo_path / path
            if not local.is_file():
                dropped.append({"file": path, "reason": "not in the checkout CI tested"})
                continue
            regs = [r for r in registrations(local.read_text(errors="replace")) if r["backend"] == backend]
            live = [r for r in regs if not r["disabled"]]
            if not live:
                dropped.append({"file": path, "reason": f"no enabled register_{backend}_ci"
                                                       + (" (disabled)" if regs else "")})
                continue
            best = min(live, key=lambda r: _TIERS.index(r["tier"]))
            est = best["est_time"]
            ci = str(item.get("ci") or "")
            by_path[path] = {"file": path, "spec": spec, "tier": best["tier"], "suite": best["suite"],
                             "rerunnable": best["rerunnable"] and not (est and est > MAX_RERUN_SECS),
                             "why": _cell(item.get("why")), "ci": ci if ci in _CI_STATES else "unknown"}
            kept.append(by_path[path])
        kept_groups = []
        for g in _items(raw.get("groups")):
            g = g if isinstance(g, dict) else {"group": g}
            name = str(g.get("group") or "")
            # /rerun-group takes a rerun_test_groups.json key or a test/registered/<dir> name.
            known = name in groups or (_GROUP_RE.match(name) and (repo_path / "test" / "registered" / name).is_dir())
            if not known:
                dropped.append({"group": name[:200], "reason": "not a group /rerun-group knows"})
            elif key == "amd" and not amd_rerun:
                dropped.append({"group": name, "reason": "/rerun-group cannot run AMD tests yet"})
            elif name not in [k["group"] for k in kept_groups]:
                kept_groups.append({"group": name, "why": _cell(g.get("why"))})
        extra_needed = raw.get("extra_needed")
        platform = {"extra_needed": extra_needed if isinstance(extra_needed, bool) else None,
                    "reason": _cell(raw.get("reason"), 600), "files": kept, "groups": kept_groups,
                    "dropped": dropped}
        platform["commands"] = commands(key, platform, amd_rerun)
        result["platforms"][key] = platform
    return result


def commands(platform: str, sel: dict, amd_rerun: bool) -> list[str]:
    """The comments to post for one platform's kept suggestions."""
    files = sel["files"]
    extra = [f for f in files if f["tier"] == "extra"]
    flag = ""
    if platform == "amd":
        if not amd_rerun:
            # Only whole workflows run on AMD until /rerun-test --amd exists.
            return ["/run-extra-ci amd"] if extra or sel["extra_needed"] else []
        flag = "--amd "
        # --amd runs per-commit and extra suites only.
        files = [f for f in files if f["tier"] in ("base", "extra")]
    out = []
    # The whole tier also covers an extra file /rerun-test cannot take.
    if len(extra) > MAX_EXTRA_FILES or (sel["extra_needed"] and not extra) or \
            any(not f["rerunnable"] for f in extra):
        out.append(f"/run-extra-ci {platform}")
        files = [f for f in files if f["tier"] != "extra"]
    files = [f for f in files if f["rerunnable"]]
    if files:
        out.append(f"/rerun-test {flag}" + " ".join(f["spec"] for f in files))
    if sel["groups"]:
        out.append(f"/rerun-group {flag}" + " ".join(g["group"] for g in sel["groups"]))
    return out


def _note(platform: str, f: dict, amd_rerun: bool) -> str:
    """Why a kept file has no command of its own, when it has none."""
    if platform == "amd" and not amd_rerun and f["tier"] != "extra":
        return " (no PR command on AMD yet; it runs in AMD PR CI)" if f["tier"] == "base" else \
            f" (runs only in AMD {f['tier']}; no PR command)"
    if platform == "amd" and f["tier"] not in ("base", "extra"):
        return f" (runs only in AMD {f['tier']}; `--amd` takes per-commit and extra suites)"
    if not f["rerunnable"] and f["tier"] != "extra":
        return " (`/rerun-test` cannot run it: over 60 min or a legacy `suite=` registration)"
    return ""


def render(result: dict) -> str:
    """The "Suggested tests" section, from the checked selection."""
    lines = ["### Suggested tests", "", f"> {DISCLAIMER}", ""]
    if not result["valid"]:
        lines += ["The agent gave no readable test selection, so there is none this time.", ""]
    else:
        for key, (name, _) in PLATFORMS.items():
            sel = result["platforms"][key]
            need = {True: "yes", False: "no", None: "not stated"}[sel["extra_needed"]]
            lines += [f"**{name}** — run extra: **{need}**. {sel['reason']}".rstrip(), ""]
            if sel["files"]:
                lines += ["| Test | Tier | Why | This CI |", "|---|---|---|---|"]
                for f in sel["files"]:
                    note = _note(key, f, result["amd_rerun"])
                    lines.append(f"| `{f['spec']}` | {f['tier']}{note} | {f['why']} | {f['ci'].replace('_', ' ')} |")
                lines.append("")
            for g in sel["groups"]:
                lines.append(f"- group `{g['group']}`: {g['why']}")
            if sel["groups"]:
                lines.append("")
            if sel["commands"]:
                lines += ["Commands (post each line as its own comment):", "", "```", *sel["commands"], "```", ""]
            elif not sel["files"] and not sel["groups"]:
                lines += ["No extra tests suggested.", ""]
    lines.append(f"> {DISCLAIMER}")
    return "\n".join(lines) + "\n"


def neutralize_outside_code(text: str, neutralize) -> str:
    """Apply *neutralize* (``neutralize_mentions``) to everything except fenced code blocks.

    GitHub pings nobody from inside a fence, and rewriting ``@decorator`` lines there garbles code.
    """
    parts = re.split(r"(^```.*?^```[^\n]*$)", text, flags=re.M | re.S)
    return "".join(p if i % 2 else neutralize(p) for i, p in enumerate(parts))
