"""Findings of the multi-agent PR review: parse, check, merge, vote, render.

The finder agents (``Task: PR Code Review``) each end with a
``<!-- review-findings {...} -->`` block and the verifier agents
(``Task: PR Code Review Verification``) with ``<!-- review-votes {...} -->``.
Everything here is deterministic, so what reaches the PR is decided in code:

  - ``check_findings``: the location must be real (a file the PR changed, a
    line in one of its hunks) and the category one the review reports;
    a convention finding must cite a maintainer rule
  - ``merge_findings``: the same bug from two angles becomes one finding
  - ``drop_repeats``: a finding an earlier review round already posted is
    not posted again
  - ``tally``: a finding is posted only if enough verifiers confirm it
"""

import json
import re
from dataclasses import dataclass, field

FINDINGS_MARK = "<!-- review-findings"
VOTES_MARK = "<!-- review-votes"
REVIEW_MARK = "<!-- amd-bot-review"

SEVERITIES = ("important", "nit")
CATEGORIES = ("correctness", "amd", "contracts", "tests", "convention", "performance", "security")
VERDICTS = ("confirmed", "refuted", "uncertain")
MAX_NITS = 3
MAX_OTHER_LOCATIONS = 8

_MENTION_RE = re.compile(r"(?<![\w`])@([A-Za-z0-9][\w-]*(?:/[\w.-]+)?)")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def neutralize_mentions(text: str) -> str:
    """Put ``@handle`` in code so a comment pings nobody."""
    return _MENTION_RE.sub(r"`@\1`", text)


def parse_block(text: str, mark: str) -> dict | None:
    """The JSON object in the last ``<mark> ... -->`` block of *text*."""
    start = (text or "").rfind(mark)
    if start == -1:
        return None
    end = text.find("-->", start)
    if end == -1:
        return None
    try:
        data = json.loads(text[start + len(mark):end])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@dataclass
class Finding:
    file: str
    line: int
    severity: str
    category: str
    title: str
    explanation: str = ""
    pre_existing: bool = False
    evidence: list[str] = field(default_factory=list)
    suggestion: str = ""
    rule: str = ""
    other_locations: list[str] = field(default_factory=list)
    angles: list[str] = field(default_factory=list)
    id: str = ""
    inline: bool = False
    confirmations: int = 0
    voters: int = 0
    vote_reasons: list[str] = field(default_factory=list)


def _norm_path(path: str) -> str:
    path = (path or "").strip().strip("`")
    for prefix in ("./", "a/", "b/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    return path


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def parse_findings(text: str, angle: str) -> list[Finding]:
    """A finder's findings. Raises ``ValueError`` if its block is missing or malformed."""
    data = parse_block(text, FINDINGS_MARK)
    if data is None or not isinstance(data.get("findings"), list):
        raise ValueError(f"reviewer '{angle}' returned no review-findings block")
    out = []
    for raw in data["findings"]:
        if not isinstance(raw, dict):
            continue
        try:
            line = int(raw.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        out.append(Finding(
            file=_norm_path(str(raw.get("file") or "")),
            line=line,
            severity=str(raw.get("severity") or "").strip().lower(),
            category=str(raw.get("category") or "").strip().lower(),
            title=" ".join(str(raw.get("title") or "").split()),
            explanation=str(raw.get("explanation") or "").strip(),
            pre_existing=bool(raw.get("pre_existing")),
            evidence=_str_list(raw.get("evidence")),
            suggestion=str(raw.get("suggestion") or "").strip(),
            rule=_norm_path(str(raw.get("rule") or "")),
            other_locations=_str_list(raw.get("other_locations"))[:MAX_OTHER_LOCATIONS],
            angles=[angle],
        ))
    return out


def diff_hunk_lines(diff: str) -> dict[str, set[int]]:
    """New-side line numbers inside each file's hunks (where GitHub accepts a comment)."""
    lines: dict[str, set[int]] = {}
    path = None
    new_no = 0
    for row in diff.splitlines():
        if row.startswith("+++ "):
            target = row[4:].strip()
            path = None if target == "/dev/null" else _norm_path(target)
            if path is not None:
                lines.setdefault(path, set())
            continue
        if row.startswith("--- ") or row.startswith("diff --git"):
            continue
        m = _HUNK_RE.match(row)
        if m:
            new_no = int(m.group(1))
            continue
        if path is None or not new_no:
            continue
        if row.startswith("+") or row.startswith(" "):
            lines[path].add(new_no)
            new_no += 1
        # "-" rows exist only on the old side; "\ No newline" rows on neither.
    return lines


def check_findings(
    findings: list[Finding],
    changed_files: set[str],
    hunk_lines: dict[str, set[int]],
    upstream_files: set[str],
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """Keep findings with a real location and a reportable category."""
    kept, dropped = [], []
    for f in findings:
        if not f.title:
            dropped.append((f, "no title"))
        elif f.severity not in SEVERITIES:
            dropped.append((f, f"severity {f.severity!r}"))
        elif f.category not in CATEGORIES:
            dropped.append((f, f"category {f.category!r} is not reported"))
        elif f.category == "convention" and f.rule not in upstream_files:
            dropped.append((f, "convention without a maintainer rule"))
        elif not f.file or f.line <= 0:
            dropped.append((f, "no file and line"))
        elif not f.pre_existing and f.file not in changed_files:
            dropped.append((f, f"{f.file} is not changed by the PR"))
        else:
            f.inline = not f.pre_existing and f.line in hunk_lines.get(f.file, set())
            kept.append(f)
    return kept, dropped


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _same(a: Finding, b: Finding) -> bool:
    if a.file != b.file:
        return False
    if _title_key(a.title) == _title_key(b.title):
        return True
    return a.category == b.category and abs(a.line - b.line) <= 3


def _rank(f: Finding) -> tuple:
    return (f.pre_existing, SEVERITIES.index(f.severity), f.file, f.line)


def merge_findings(findings: list[Finding]) -> list[Finding]:
    """One finding per bug: duplicates from several angles are merged, then numbered."""
    merged: list[Finding] = []
    for f in sorted(findings, key=_rank):
        same = next((m for m in merged if _same(m, f)), None)
        if same is None:
            merged.append(f)
            continue
        for angle in f.angles:
            if angle not in same.angles:
                same.angles.append(angle)
        same.evidence += [e for e in f.evidence if e not in same.evidence]
        same.other_locations += [o for o in f.other_locations if o not in same.other_locations]
        same.other_locations = same.other_locations[:MAX_OTHER_LOCATIONS]
    merged.sort(key=_rank)
    for i, f in enumerate(merged, 1):
        f.id = f"F{i}"
    return merged


def fingerprint(f: Finding) -> dict:
    return {"file": f.file, "line": f.line, "category": f.category, "title": f.title}


def drop_repeats(
    findings: list[Finding], previous: list[dict],
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """Leave out findings an earlier round already posted."""
    earlier = [
        Finding(file=_norm_path(p.get("file", "")), line=int(p.get("line") or 0),
                severity="nit", category=p.get("category", ""), title=p.get("title", ""))
        for p in previous if isinstance(p, dict)
    ]
    kept, dropped = [], []
    for f in findings:
        if any(_same(e, f) for e in earlier):
            dropped.append((f, "posted in an earlier round"))
        else:
            kept.append(f)
    return kept, dropped


def cap_findings(
    findings: list[Finding], max_findings: int,
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """At most *max_findings* go to the verifiers, and at most ``MAX_NITS`` nits."""
    kept, dropped = [], []
    nits = 0
    for f in sorted(findings, key=_rank):
        if f.severity == "nit" and nits >= MAX_NITS:
            dropped.append((f, f"over {MAX_NITS} nits"))
        elif len(kept) >= max_findings:
            dropped.append((f, f"over {max_findings} findings"))
        else:
            nits += f.severity == "nit"
            kept.append(f)
    return kept, dropped


def parse_votes(text: str) -> dict[str, dict]:
    """A verifier's votes by finding id. Raises ``ValueError`` if its block is missing."""
    data = parse_block(text, VOTES_MARK)
    if data is None or not isinstance(data.get("votes"), list):
        raise ValueError("verifier returned no review-votes block")
    votes = {}
    for v in data["votes"]:
        if isinstance(v, dict) and str(v.get("verdict", "")).lower() in VERDICTS and v.get("id"):
            votes[str(v["id"])] = {
                "verdict": str(v["verdict"]).lower(),
                "severity": str(v.get("severity") or "").lower(),
                "pre_existing": v.get("pre_existing"),
                "reason": " ".join(str(v.get("reason") or "").split()),
            }
    return votes


def tally(
    findings: list[Finding], ballots: list[dict[str, dict]], required: int,
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """Keep findings at least *required* verifiers confirmed.

    A label (severity, pre-existing) changes only when a majority of the
    confirming verifiers gives the new one; otherwise the finder's label
    stands. A missing vote counts as not confirmed.
    """
    if len(ballots) < required:
        raise RuntimeError(
            f"only {len(ballots)} verifier(s) finished; {required} must confirm a finding"
        )
    kept, dropped = [], []
    for f in findings:
        votes = [b[f.id] for b in ballots if f.id in b]
        yes = [v for v in votes if v["verdict"] == "confirmed"]
        f.confirmations, f.voters = len(yes), len(ballots)
        f.vote_reasons = [v["reason"] for v in votes if v["reason"]]
        if len(yes) < required:
            dropped.append((f, f"confirmed by {len(yes)}/{len(ballots)} verifiers"))
            continue
        sev = [v["severity"] for v in yes]
        for label in SEVERITIES:
            if sev.count(label) * 2 > len(yes):
                f.severity = label
        pre = [v["pre_existing"] for v in yes]
        if pre.count(True) * 2 > len(yes):
            f.pre_existing, f.inline = True, False
        elif pre.count(False) * 2 > len(yes):
            f.pre_existing = False
        kept.append(f)
    return kept, dropped


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _label(f: Finding) -> str:
    if f.pre_existing:
        return "⚪ **Pre-existing**"
    return "🔴 **Important**" if f.severity == "important" else "🟡 **Nit**"


def _details(f: Finding) -> list[str]:
    lines = []
    if f.explanation:
        lines += ["", neutralize_mentions(f.explanation)]
    if f.suggestion:
        lines += ["", f"**Suggestion:** {neutralize_mentions(f.suggestion)}"]
    if f.other_locations:
        lines += ["", "Same pattern also at: " + ", ".join(f"`{o}`" for o in f.other_locations)]
    if f.rule:
        lines += ["", f"Rule: `{f.rule.removeprefix('upstream/')}` (sglang maintainers)"]
    if f.evidence:
        lines += ["", "<details><summary>Evidence</summary>", ""]
        lines += [f"- {neutralize_mentions(e)}" for e in f.evidence]
        lines += ["", "</details>"]
    lines += ["", f"<sub>{f.category} · confirmed by {f.confirmations}/{f.voters} verifiers</sub>"]
    return lines


def render_inline(f: Finding) -> str:
    """Body of the inline comment on ``f.file:f.line``."""
    return "\n".join([f"{_label(f)} · {neutralize_mentions(f.title)}", *_details(f)])


def render_listed(f: Finding, repo: str, sha: str) -> str:
    """A finding listed in the summary (no inline place, or pre-existing)."""
    url = f"https://github.com/{repo}/blob/{sha}/{f.file}#L{f.line}"
    head = f"#### {_label(f)} · [`{f.file}:{f.line}`]({url}) · {neutralize_mentions(f.title)}"
    return "\n".join([head, *_details(f)])


def count_line(findings: list[Finding]) -> str:
    """The summary's first line: a count, or plainly that there is nothing to fix."""
    new = [f for f in findings if not f.pre_existing]
    pre = len(findings) - len(new)
    if not new:
        line = "**No issues found** in this PR's changes."
    else:
        imp = sum(f.severity == "important" for f in new)
        line = (f"**{len(new)} issue{'s' if len(new) != 1 else ''}** · "
                f"{imp} Important · {len(new) - imp} Nit")
    if pre:
        line += f" · {pre} pre-existing (not introduced here)"
    return line


def render_marker(meta: dict) -> str:
    return f"{REVIEW_MARK} {json.dumps(meta, separators=(',', ':'))} -->"


def parse_marker(body: str) -> dict | None:
    return parse_block(body or "", REVIEW_MARK)
