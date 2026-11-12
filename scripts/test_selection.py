"""Suggested tests in the ci-status report: sglang's own selection method plus a code gate.

The ci-status agent picks the tests a PR should run, following sglang main's
``rerun-test`` and ``write-sglang-test`` skills (copied from ``main`` into
``.ci-context/sglang-skills/``, never read from the PR checkout, which the PR
can change). It ends its report with a hidden ``test-selection`` block. Code
keeps only the files that exist, are registered for the platform and are not
disabled, then writes the commands, so the section never asks anyone to run a
test that cannot run. The bot only suggests; it triggers nothing (R3).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

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
DISCLAIMER = "🤖 AI-generated suggestion — use it with your own judgement."
PLATFORMS = {"nv": ("NVIDIA", "cuda"), "amd": ("AMD", "amd")}
_REGISTER_RE = re.compile(r"^register_(\w+)_ci$")
_BLOCK_RE = re.compile(r"<!--\s*test-selection\s*(\{.*?\})\s*-->", re.DOTALL)
_CI_STATES = ("passed", "failed", "not_run", "pending")


def main_context(token: str, fetch=None) -> tuple[dict[str, str], bool]:
    """The skill files from sglang ``main`` as context files, and whether ``/rerun-test --amd`` exists there."""
    fetch = fetch or get_file_content_at_ref
    files = {}
    for name, path in SKILL_FILES.items():
        text = fetch(token, path, "main")
        if text:
            files[f"sglang-skills/{name}"] = text
    handler = fetch(token, HANDLER_PATH, "main") or ""
    return files, bool(re.search(r"""=\s*["']--amd["']""", handler))


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
    """Module-level ``register_<backend>_ci(...)`` calls, read the way ``test/run_suite.py`` reads them."""
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
        kw = {k.arg: k.value.value for k in call.keywords
              if k.arg and isinstance(k.value, ast.Constant)}
        if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
            kw.setdefault("suite", call.args[1].value)
        if kw.get("stage") and kw.get("runner_config"):
            suite = f"{kw['stage']}-test-{kw['runner_config']}"
        else:
            suite = str(kw.get("suite") or "")
        est = kw.get("est_time", call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) else None)
        out.append({"backend": m.group(1), "suite": suite, "tier": tier(suite, kw.get("nightly") is True),
                    "disabled": bool(kw.get("disabled")), "est_time": est})
    return out


def _normalize(entry: str) -> tuple[str, str]:
    """``(file path from the repo root, spec for /rerun-test)`` from what the agent wrote."""
    entry = entry.strip().strip("`")
    path, sep, rest = entry.partition("::")
    path = path.lstrip("./")
    if path.startswith("registered/"):
        path = "test/" + path
    spec = path.removeprefix("test/") + (f"::{rest}" if sep and rest else "")
    return path, spec


def _cell(value, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return (text[:limit] + "…" if len(text) > limit else text).replace("|", "\\|")


def check(block: dict | None, repo_path: Path, token: str, groups: set[str], amd_rerun: bool,
          fetch=None) -> dict:
    """Keep the suggested files that exist, are registered for the platform and are not disabled.

    A file is read from the checkout CI tested (the PR's own tests) and, when
    absent there, from ``main``. At most ``MAX_FILES`` per platform are kept.
    Returns, per platform, what is kept, what was dropped and why, and the commands.
    """
    fetch = fetch or get_file_content_at_ref
    result = {"amd_rerun": amd_rerun, "valid": block is not None, "platforms": {}}
    for key, (_, backend) in PLATFORMS.items():
        raw = (block or {}).get(key) if isinstance(block, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        kept, dropped, seen = [], [], set()
        for item in raw.get("files") or []:
            item = item if isinstance(item, dict) else {"file": item}
            path, spec = _normalize(str(item.get("file") or ""))
            if not path.endswith(".py") or ".." in path.split("/"):
                dropped.append({"file": path, "reason": "not a test file path"})
                continue
            if path in seen:
                continue
            seen.add(path)
            if len(kept) >= MAX_FILES:
                dropped.append({"file": path, "reason": f"over the limit of {MAX_FILES} files"})
                continue
            local = repo_path / path
            text = local.read_text(errors="replace") if local.is_file() else fetch(token, path, "main")
            if text is None:
                dropped.append({"file": path, "reason": "not in the tested checkout or on main"})
                continue
            regs = [r for r in registrations(text) if r["backend"] == backend]
            live = [r for r in regs if not r["disabled"]]
            if not live:
                dropped.append({"file": path, "reason": f"no enabled register_{backend}_ci"
                                                       + (" (disabled)" if regs else "")})
                continue
            best = min(live, key=lambda r: ("base", "extra", "nightly", "weekly", "other").index(r["tier"]))
            ci = str(item.get("ci") or "")
            kept.append({"file": path, "spec": spec, "tier": best["tier"], "suite": best["suite"],
                         "why": _cell(item.get("why")), "ci": ci if ci in _CI_STATES else "unknown"})
        kept_groups = []
        for g in raw.get("groups") or []:
            g = g if isinstance(g, dict) else {"group": g}
            name = str(g.get("group") or "")
            if name not in groups:
                dropped.append({"group": name, "reason": "not a group in main's rerun_test_groups.json"})
            elif key == "amd" and not amd_rerun:
                dropped.append({"group": name, "reason": "/rerun-group cannot run AMD tests yet"})
            else:
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
    whole_extra = len(extra) > MAX_EXTRA_FILES or (sel["extra_needed"] and not extra)
    if whole_extra:
        out.append(f"/run-extra-ci {platform}")
        files = [f for f in files if f["tier"] != "extra"]
    if files:
        out.append(f"/rerun-test {flag}" + " ".join(f["spec"] for f in files))
    if sel["groups"]:
        out.append(f"/rerun-group {flag}" + " ".join(g["group"] for g in sel["groups"]))
    return out


def _no_command_note(platform: str, f: dict, amd_rerun: bool) -> str:
    if platform == "amd" and not amd_rerun and f["tier"] != "extra":
        return " (no PR command on AMD yet; it runs in AMD PR CI)" if f["tier"] == "base" else \
            f" (runs only in AMD {f['tier']}; no PR command)"
    if platform == "amd" and f["tier"] not in ("base", "extra"):
        return f" (runs only in AMD {f['tier']}; `--amd` takes per-commit and extra suites)"
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
                    note = _no_command_note(key, f, result["amd_rerun"])
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
