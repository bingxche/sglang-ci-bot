# Rules for changing sglang-ci-bot

These rules apply to every change, whether typed by hand or written by an AI tool. The goal is a repo that stays understandable however many people and agents change it. `pr-checks.yml` enforces the checkable parts on every pull request, whoever or whatever wrote the code.

**This file is the only place these rules live.**
- Codex, Cursor, GitHub Copilot, Windsurf, Zed and Jules read `AGENTS.md` natively.
- Claude Code reads `CLAUDE.md`, which is the one line `@AGENTS.md`.
- Other tools can be pointed here too; for example, Gemini CLI can be set to read `AGENTS.md` instead of `GEMINI.md`.

Never write rules into a tool-specific file (`CLAUDE.md`, `GEMINI.md`, `.cursor/rules/`, `.github/copilot-instructions.md`, …). Such a file may only point here. `tests/test_agent_rules.py` fails otherwise, so there is never a second copy to keep in sync.

(These rules are for people and tools changing this repo. The bot's own agents never read them: their instructions are `agent/ground-rules.md` plus `agent/tasks/`, passed explicitly.)

## 1. Read the map first

`README.md` is the **only** architecture, usage and feature documentation for this repository. Before changing anything, read its §1 (the big picture), §2 (feature map) and §3 (design rules R1–R11), then the section of the feature you are changing.

## 2. Stay on direction

- **Follow the design rules (README §3).** They are the contract the rest of the design assumes. Don't break one silently. If a change must bend a rule, name it in the PR's `Design rules:` line, say why, and update the rule in the README. Changing a rule needs the repo owner's approval.
- **Extend the existing patterns instead of adding parallel ones:**
  - agents run through `claude_code_analyze()` and `AGENT_TASKS`;
  - state lives in hidden markers on GitHub;
  - anything published passes a code gate;
  - new upstream writers start in dry-run behind a repository variable.
- **One concern per PR.** No drive-by refactors, renames, moves or reformatting, so every diff can be reviewed by reading it.

## 3. Agent instructions live only in `agent/`

All AI behavior, in agent mode (Claude Code CLI) and API mode (direct LLM calls), is defined under `agent/`:
- the Ground Rules shared by every task: `agent/ground-rules.md`;
- one file per task: `agent/tasks/`;
- the API-mode templates: `agent/api-prompts.md`.

Do NOT hardcode prompts or instructions in Python scripts. Scripts pass only data (task type, metadata, URLs).

Keep `agent/ground-rules.md` short: every agent run carries it. A rule that only one task needs belongs in that task's file. A new `Task:` needs a file in `agent/tasks/` and an entry in `AGENT_TASKS` (`scripts/utils.py`), which also sets its model.

A change to an agent's instructions or model must be run on real data before merge, with `scripts/validate_live.py`, a CI-status replay, or a linked run. Say how in the PR's `Validated:` line (design rule R10).

## 4. Update the README in the same PR

Any change to behavior (a feature, refactor, fix, config, prompt or workflow) updates `README.md` in the same PR:

- **New feature, script, workflow or trigger**: add a lane to the diagram in §1, a row to the feature map in §2, and a section in §5.
- **Changed behavior of a feature**: update its section in §5 (purpose, flow, things to know).
- **New or changed agent task, instructions or model**: update the task table in §4.
- **New hidden marker or other state on GitHub**: add it to the markers table in §3.
- **Changed infrastructure** (runner, Docker, tokens, secrets, variables): update §6.
- **Changed environment variables or CLI options**: update §7.
- **Renamed or moved functions or files**: update the links that point at them.

How the README is written:
- It explains **what** each part does and **why**.
- It links to the code for **how**, as file + function name. Never use line numbers; they go stale on the next edit.
- It does not copy lists that live in code. Name the constant and link it; `tests/test_readme.py` checks the one list it does show.

If unsure whether a README update is needed, update it. Outdated docs are worse than no docs.

## 5. What the pull request must contain

Fill in the three lines of `.github/pull_request_template.md`:

```
README: <sections updated, or "not needed — reason">
Design rules: <none | rule IDs touched, e.g. "R2, R4 — what changes and why">
Validated: <for agent/ or model changes: how it ran on real data>
```

`pr-checks.yml` fails the PR when:
- the unit tests fail. These include `tests/test_readme.py`, which checks that the README's links, function names and lists match the code, and that every script, workflow, agent task and environment variable is mentioned;
- `scripts/check_pr_rules.py` finds a problem:
  - a behavior change without a README change or a reason;
  - a missing or unknown `Design rules:` entry;
  - an agent change without a `Validated:` line.

Run the tests locally before pushing:

```bash
PYTHONPATH=scripts python3 -m unittest discover -s tests -p 'test_*.py'
```
