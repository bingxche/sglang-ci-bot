"""GitHub credential helpers for the temporary ``bingxche`` publisher.

``SGLANG_PAT`` names the credential used against ``sgl-project/sglang``.
During the emergency fallback it may be the existing ``bingxche`` classic PAT
because GitHub does not let an outside collaborator create an organization-
scoped fine-grained PAT.  The bot still limits its own upstream writes to
issue comments/reactions (plus the existing Failure Tracker issue updates),
and production workflows never launch an analysis agent while holding it.

Inside GitHub Actions, ``BOT_REPO_TOKEN`` should be the short-lived
``github.token``.  The long-lived watcher daemon may temporarily reuse the
same ``bingxche`` credential so only one personal token is required.
"""

from __future__ import annotations

import logging
import os

import requests


SGLANG_REPO = "sgl-project/sglang"
HTTP_TIMEOUT = (10, 30)
log = logging.getLogger(__name__)


def sglang_token_from_env() -> str:
    """Return the upstream credential, accepting the legacy env during rollout."""
    return (
        os.environ.get("SGLANG_PAT", "").strip()
        or os.environ.get("GH_PAT", "").strip()
    )


def bot_repo_token_from_env() -> str:
    """Return a credential scoped to the bot repository only.

    In GitHub Actions this is normally the short-lived ``GITHUB_TOKEN``.  The
    long-lived daemon temporarily reuses the upstream credential.
    """
    return (
        os.environ.get("BOT_REPO_TOKEN", "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
        or sglang_token_from_env()
    )


def require_distinct_tokens(sglang_token: str, bot_repo_token: str) -> None:
    """Warn when the emergency daemon reuses one token for both repositories."""
    if sglang_token and bot_repo_token and sglang_token == bot_repo_token:
        log.warning(
            "Emergency single-token mode: the same bingxche credential is "
            "being used for upstream reads/comments and bot-repo dispatch"
        )


def validate_sglang_token(
    token: str,
    expected_login: str = "bingxche",
    *,
    session=requests,
) -> str:
    """Validate that the emergency publisher authenticates as ``bingxche``.

    GitHub does not expose an effective permission manifest for a token.  A
    classic PAT is accepted here only because outside collaborators cannot
    create an organization-scoped fine-grained PAT.  Runtime code and workflow
    tests provide the write boundary; this function provides the identity
    boundary.
    """
    if not token:
        raise ValueError("SGLANG_PAT is required")

    response = session.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()

    login = (response.json().get("login") or "").strip()
    if not login:
        raise ValueError("SGLANG_PAT did not return an authenticated GitHub login")
    if expected_login and login.lower() != expected_login.lower():
        raise ValueError(
            f"SGLANG_PAT authenticates as {login!r}, expected {expected_login!r}"
        )

    # Classic PATs advertise OAuth scopes.  Log the emergency exception without
    # ever printing the credential itself.
    oauth_scopes = {
        scope.strip().lower()
        for scope in response.headers.get("X-OAuth-Scopes", "").split(",")
        if scope.strip()
    }
    broad = oauth_scopes & {"repo", "public_repo", "workflow"}
    if broad:
        log.warning(
            "Authenticated @%s with an emergency classic PAT (%s); analysis "
            "is pinned to API mode in production workflows",
            login,
            ", ".join(sorted(broad)),
        )
    return login
