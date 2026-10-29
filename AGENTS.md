# Agent Rules for sglang-ci-bot

## README is the single source of documentation

`README.md` is the **only** architecture, usage, and feature documentation for this repository. There are no other docs files.

When you make **any** change to the codebase — new feature, refactoring, bug fix, config change, prompt update, workflow change — you MUST update `README.md` to reflect that change in the same commit.

Specifically:

- **New feature or script**: Add to the Features table and CLI usage section
- **Changed agent behavior**: Update "How Agent Mode Works" and the CLAUDE.md description in "Project structure"
- **Changed prompts or output format**: Update the relevant section describing AI behavior
- **Changed infrastructure** (runner, Docker, workflows): Update Setup, Container isolation, or Concurrency sections
- **Changed CLI options**: Update the CLI usage tables
- **Changed environment variables**: Update the Environment variables table
- **Refactoring** (moved functions, renamed files): Update "Project structure"

If unsure whether a README update is needed, update it. Outdated docs are worse than no docs.

## agent/ is the single source of AI instructions

All AI behavior — both agent mode (Claude Code CLI) and API mode (direct LLM calls) — is defined under `agent/`: the Ground Rules shared by every task in `agent/CLAUDE.md`, one file per task in `agent/tasks/`, and the API-mode templates in `agent/api-prompts.md`. Do NOT hardcode prompts or instructions in Python scripts. Scripts should only pass data (task type, metadata, URLs) to the AI; all methodology, output format, and constraints live in `agent/`.

Keep `agent/CLAUDE.md` short: every agent run carries it. A rule that only one task needs belongs in that task's file. A new `Task:` needs a file in `agent/tasks/` and an entry in `AGENT_TASKS` (`scripts/utils.py`), which also sets its model.
