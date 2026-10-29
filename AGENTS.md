# Agent Rules for sglang-ci-bot

## README is the map of this repo

`README.md` is the **only** architecture, usage, and feature documentation for this repository. There are no other docs files. It explains what each part does and why, and links to the code (file + function name, never line numbers) for how.

When you make **any** change to the codebase — new feature, refactoring, bug fix, config change, prompt update, workflow change — you MUST update `README.md` to reflect that change in the same commit.

Specifically:

- **New feature, script, workflow or trigger**: Add it to the diagram in §1, the feature map in §2, and give it a section in §5.
- **Changed behavior of a feature**: Update its section in §5 (purpose, flow, things to know).
- **New or changed agent task, instructions or model**: Update the task table in §4.
- **New hidden marker or other state on GitHub**: Add it to the markers table in §3.
- **Changed infrastructure** (runner, Docker, tokens, secrets, variables): Update §6.
- **Changed environment variables or CLI options**: Update §7.
- **Renamed or moved functions or files**: Update the links that point at them.

**Design rules (§3) are the contract.** If a change breaks or bends one (for example, code that publishes something no agent output was checked for, an agent with write access, or a write upstream without a gate), say so plainly in the PR description, and update the rule only if the change is deliberate.

`tests/test_readme.py` checks that the README's links, function names and monitored-workflow list match the code, and that every script, workflow, agent task and environment variable is mentioned. It must pass.

If unsure whether a README update is needed, update it. Outdated docs are worse than no docs.

## agent/ is the single source of AI instructions

All AI behavior — both agent mode (Claude Code CLI) and API mode (direct LLM calls) — is defined under `agent/`: the Ground Rules shared by every task in `agent/CLAUDE.md`, one file per task in `agent/tasks/`, and the API-mode templates in `agent/api-prompts.md`. Do NOT hardcode prompts or instructions in Python scripts. Scripts should only pass data (task type, metadata, URLs) to the AI; all methodology, output format, and constraints live in `agent/`.

Keep `agent/CLAUDE.md` short: every agent run carries it. A rule that only one task needs belongs in that task's file. A new `Task:` needs a file in `agent/tasks/` and an entry in `AGENT_TASKS` (`scripts/utils.py`), which also sets its model.
