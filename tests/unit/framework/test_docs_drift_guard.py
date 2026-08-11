"""Guard the docs against the half-renames a big vocabulary change leaves behind.

Deliberately narrow: it fails on phrases that are **genuinely dead** after the
harness/provider split, not on everything containing the word "token". Three things stay
legal and are allowlisted rather than banned:

- ``agent_view/provider`` — still a correct config path, only its semantics changed
  (``anthropic`` instead of ``claude``); banning the name would be plain wrong;
- ``token:*`` — documented compatibility aliases, deliberately described as deprecated;
- ``sandbox_packages`` — the legacy ``di.json`` section, read for one more cycle.

``docs/migrations/**`` and ``docs/superpowers/plans/**`` are explicitly historical
records of what the system *used to be*, so they are exempt entirely.

This test exists because the first documentation pass silently left five `token:*` rows
and a "Tokens" heading in ``docs/cli/README.md`` — exactly the failure mode it catches.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

# Directories whose whole point is to record superseded state.
EXEMPT_PREFIXES = ("docs/migrations/", "docs/superpowers/")

# Phrase → files where an occurrence is EXPECTED (relative paths, prefix match).
# A phrase with an empty allowlist is banned everywhere.
DEAD_PHRASES: dict[str, tuple[str, ...]] = {
    # The closed enum and the five registries it keyed.
    r"\bAgentProvider\b": (
        "DECISIONS.md",                          # the decision that removed it
        "docs/architecture/harness-contract.md",  # explains what it replaced
        "ROADMAP.md",                            # historical phase notes
    ),
    r"\bConfigWriter\b": ("DECISIONS.md", "docs/architecture/harness-contract.md"),
    r"\bCliInvoker\b": (
        "DECISIONS.md",
        "docs/architecture/harness-contract.md",
        "docs/cli/run.md",                        # explains the flag drift it caused
    ),
    # ROADMAP.md's completed-phase list records what each phase DELIVERED at the time —
    # a historical log, same category as docs/migrations/**.
    r"\bAuthStrategy\b": (
        "DECISIONS.md", "docs/architecture/harness-contract.md", "ROADMAP.md",
    ),
    # di.json sections that no longer exist.
    r"\bcli_invokers\b": (
        "docs/architecture/harness-contract.md",
        "docs/modules/module-json.md",             # lists what agent_harnesses replaced
    ),
    r"\bconfig_writers\b": (
        "docs/modules/module-json.md",            # lists what agent_harnesses replaced
        "docs/architecture/harness-contract.md",
    ),
    # Renamed modules.
    r"\btoken_store\b": (),
    r"\btoken_resolver\b": (),
    # Renamed table / column.
    r"\boauth_token\b": (
        "docs/cli/credentials.md",                # names the old table in the rename note
        "docs/architecture/harness-contract.md",
        "DECISIONS.md",
        "ROADMAP.md",
        "docs/config/identity.md",
        "AGENTS.md",                              # "credential (ex-oauth_token)" note
    ),
    r"usage_log\.token_id": (),
    # The removed di.json SECTION only — not the ordinary English word, which ROADMAP.md
    # uses for "execution runtimes". Matching the bare word here would fail on correct prose,
    # which is exactly the over-broad guard the plan warns against.
    r'"runtimes"\s*:': (),
    r"\bruntimes from di\.json": (),
    r"\bruntimes`? *(?:,|and) *`?config_writers": (
        "docs/architecture/harness-contract.md", "docs/modules/module-json.md",
    ),
    # Operator-facing command names that no longer exist as the primary spelling.
    r"token:register\s+claude": (),
    r"agent provider selection": (),
    # Operator-facing surfaces that outlived the rename (round 8).
    r"^#+ .*\bTokens\b": (),                    # a "Tokens" heading
    r"\bagento token (?:register|set)\b": (),   # commands that never existed post-rename
    # The dead ACTION, with or without intervening words ("Set selected token as primary").
    # Requiring "token" between the two keeps it from firing on prose that documents the
    # absence ("there is no 'set primary' action", "no sticky-primary fallback") — which is
    # correct documentation, not drift.
    r"[Ss]et\b[^.\n]{0,40}\btoken\b[^.\n]{0,25}\bprimary\b": (),
    r"\bprimary token\b": ("docs/cli/credentials.md", "DECISIONS.md", "ROADMAP.md"),
}

# Source files that print operator-facing command names — a stale hint here is as
# misleading as a stale doc, and the docs sweep above does not read *.py.
COMMAND_HINT_SOURCES = (
    "src/agento/framework/cli/install.py",
    "src/agento/framework/cli/compose.py",
)

# Phrases that MUST stay allowed — asserted below so a future tightening of the guard
# cannot quietly ban something the project still documents on purpose.
STILL_LEGAL = ("agent_view/provider", "token:register", "sandbox_packages")


def _docs() -> list[Path]:
    files = sorted(REPO.glob("docs/**/*.md"))
    # README.md is the FIRST operator surface, so it must be swept too — round 8 found it
    # (and admin.md, getting-started.md, identity.md) still saying "Tokens".
    files += [
        REPO / "README.md", REPO / "AGENTS.md", REPO / "ROADMAP.md", REPO / "DECISIONS.md",
    ]
    return [f for f in files if f.exists()]


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _is_exempt(rel: str) -> bool:
    return rel.startswith(EXEMPT_PREFIXES)


@pytest.mark.parametrize("phrase", sorted(DEAD_PHRASES))
def test_dead_phrase_appears_only_where_expected(phrase: str):
    allowed = DEAD_PHRASES[phrase]
    pattern = re.compile(phrase)
    offenders: list[str] = []

    for path in _docs():
        rel = _rel(path)
        if _is_exempt(rel) or (allowed and rel.startswith(allowed)):
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")

    assert not offenders, (
        f"dead phrase {phrase!r} found outside its allowlist {list(allowed)}:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("phrase", STILL_LEGAL)
def test_intentionally_kept_phrase_is_not_banned(phrase: str):
    """These are compatibility surfaces, not drift — the guard must not ban them."""
    for pattern in DEAD_PHRASES:
        assert not re.search(pattern, phrase), (
            f"{phrase!r} is a documented compatibility surface but matches the dead "
            f"pattern {pattern!r} — the guard would fail on correct docs"
        )


def test_credentials_doc_replaced_the_tokens_doc():
    assert (REPO / "docs/cli/credentials.md").exists()
    assert not (REPO / "docs/cli/tokens.md").exists()


def test_no_doc_links_to_the_renamed_token_doc():
    offenders = [
        f"{_rel(p)}"
        for p in _docs()
        if not _is_exempt(_rel(p)) and "cli/tokens.md" in p.read_text()
    ]
    assert offenders == [], f"stale link to docs/cli/tokens.md in: {offenders}"


@pytest.mark.parametrize("source", COMMAND_HINT_SOURCES)
def test_source_files_do_not_print_removed_command_names(source: str):
    """`agento install` used to print `agento token:register claude` — a name that is now
    only a hidden alias, with a harness id where a scope belongs."""
    text = (REPO / source).read_text()
    for dead in ("token:register claude", "token:register <agent>"):
        assert dead not in text, f"{source} prints the removed command form {dead!r}"


def test_harness_contract_doc_is_indexed():
    """A new architecture doc nobody links to is a doc nobody reads."""
    index = (REPO / "docs/architecture/README.md").read_text()
    assert "harness-contract.md" in index
