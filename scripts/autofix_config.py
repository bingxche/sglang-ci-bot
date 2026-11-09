"""ci-autofix's switch and limits, in one place.

The switch is the repository variable ``AUTOFIX_MODE``, read by
``mode_from_env()``: ``off`` (the default) does nothing at all, ``live`` runs.
Nothing else needs setting; ``AUTOFIX_MAX_PER_HOUR`` is optional (default 3).
The rest are constants: whose name autofix writes under, which workflows it
fixes, attempts, agent budgets, and size limits.
"""

import os

from utils import REPO

MODES = ("off", "live")
DEFAULT_MODE = "off"
DEFAULT_MAX_PER_HOUR = 3
MAX_ATTEMPTS = 2

# The account whose token pushes, dispatches and opens the draft PRs. It is the
# only person the queue notice @-mentions, as the owner of the switch (R2), and
# every branch the bot pushes, dispatches on or deletes starts with its name.
AUTOFIX_LOGIN = "bingxche"
NOTIFY_USER = AUTOFIX_LOGIN
BRANCH_PREFIX = f"{AUTOFIX_LOGIN}/autofix-"
API = f"https://api.github.com/repos/{REPO}"

# Agent budgets: effort max is slow, so these are generous (R9).
AGENT_TIMEOUT_SECS = int(os.environ.get("AUTOFIX_AGENT_TIMEOUT_SECS", "7200"))
AGENT_MAX_TURNS = int(os.environ.get("AUTOFIX_AGENT_MAX_TURNS", "400"))
REVIEW_TIMEOUT_SECS = int(os.environ.get("AUTOFIX_REVIEW_TIMEOUT_SECS", "3600"))
REVIEW_MAX_TURNS = int(os.environ.get("AUTOFIX_REVIEW_MAX_TURNS", "150"))
# A dispatched run that has not finished its jobs by then is given up.
DISPATCH_TIMEOUT_HOURS = 24
# Scheduled runs read back per job when checking that a failure is not flaky, and how
# many of the workflow's latest scheduled runs are looked through to find them.
HISTORY_RUNS = 6
HISTORY_SCAN = 15
# Open PRs whose files are scanned for the test file, newest first.
MAX_OPEN_PRS_SCANNED = 300
# Words the PR's prose may have (links and code blocks not counted): the spec's
# "about 250", with 10% slack so a precise, hedged text is not rejected at 262.
MAX_BODY_WORDS = 275
# GitHub rejects comments over 65,536 characters; the log comment carries the state.
MAX_COMMENT_CHARS = 60_000
MAX_STATE_DIFF = 6_000
MAX_PREVIOUS = 12_000
# A model-unavailable problem gives up after this many hours, so it stops taking a slot.
MAX_UNAVAILABLE = 3

PR_TEST_WORKFLOW = "pr-test-amd.yml"
PR_TEST_EXTRA_WORKFLOW = "pr-test-amd-extra.yml"
NIGHTLY_WORKFLOW = "nightly-test-amd.yml"
DISAGG_WORKFLOW = "nightly-amd-mi355x-disagg.yml"
# Workflows whose scheduled failures the bot fixes.
AUTOFIX_WORKFLOWS = (PR_TEST_WORKFLOW, NIGHTLY_WORKFLOW, DISAGG_WORKFLOW)
# Monitored workflows the bot leaves alone, and why.
NOT_FIXED = {
    "nightly-test-amd-miles-rocm10.yml": "tests the sglang inside the Miles image, not a branch",
    "nightly-test-amd-miles-rocm10-mi30x.yml": "tests the sglang inside the Miles image, not a branch",
    "amd-aiter-scout.yml": "tests AITER main; not a scheduled failure of sglang main",
}


def mode_from_env() -> str:
    """The switch: ``AUTOFIX_MODE``, ``off`` unless it says ``live``."""
    mode = (os.environ.get("AUTOFIX_MODE") or DEFAULT_MODE).strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def max_per_hour_from_env() -> int:
    return int(os.environ.get("AUTOFIX_MAX_PER_HOUR") or DEFAULT_MAX_PER_HOUR)
