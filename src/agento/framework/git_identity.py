"""Git commit author identity — single source of truth (agent-agnostic).

The agent commits with ``git`` inside the runner; the SSH key authenticates the
*push* but never sets the author. The author/committer come from git config, which
``workspace:build`` materializes into ``~/.gitconfig`` ``[user]`` from the agent_view's
``identity/git_author_*`` config — AND, because a repo-local ``.git/config [user]``
overrides a global ``~/.gitconfig``, the same values are also exported as
``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` env vars on the agent process. Git env vars take
precedence over every config file (system/global/repo-local), so the configured
identity wins even in a clone that carries its own ``[user]``.

Values are single-line-sanitized (control chars stripped) before use; the gitconfig
serializer additionally double-quotes/escapes so a value can never inject extra INI.
"""
from __future__ import annotations

GIT_AUTHOR_NAME_PATH = "agent_view/identity/git_author_name"
GIT_AUTHOR_EMAIL_PATH = "agent_view/identity/git_author_email"


def clean_identity_value(raw: str) -> str:
    """Single-line a value: drop control chars (NUL/CR/LF/etc. and DEL), trim ends."""
    return "".join(c for c in raw if c >= " " and c != "\x7f").strip()


def _quote(cleaned: str) -> str:
    """git's own double-quoted value encoding (escapes ``\\`` and ``"``)."""
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'


def gitconfig_user_block(name_raw: str, email_raw: str) -> str | None:
    """Return a ``[user]`` gitconfig block (quoted/escaped) or None if both empty."""
    name = clean_identity_value(name_raw)
    email = clean_identity_value(email_raw)
    if not (name or email):
        return None
    lines = ["[user]\n"]
    if name:
        lines.append(f"\tname = {_quote(name)}\n")
    if email:
        lines.append(f"\temail = {_quote(email)}\n")
    return "".join(lines)


def git_identity_env(name_raw: str, email_raw: str) -> dict[str, str]:
    """``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` env vars from the identity config.

    These override ALL gitconfig levels (including repo-local ``.git/config``), so the
    agent's commits are authored correctly even in a reused clone whose ``.git/config``
    carries a stale ``[user]``. Empty fields are omitted (no override for that field).
    """
    name = clean_identity_value(name_raw)
    email = clean_identity_value(email_raw)
    env: dict[str, str] = {}
    if name:
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_COMMITTER_NAME"] = name
    if email:
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_EMAIL"] = email
    return env
