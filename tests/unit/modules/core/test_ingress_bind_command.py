"""Tests for the ingress:bind CLI command — regex validation (SEC-F2/SEC-F3) + priority."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import regex

from agento.framework import ingress_identity as ii
from agento.framework.ingress_identity import match_ingress_identities, register_regex_identity_type
from agento.modules.core.src.commands.ingress_bind import IngressBindCommand

REGEX_TYPE = "outlook_sender"


@pytest.fixture
def regex_type():
    saved = set(ii._REGEX_IDENTITY_TYPES)
    register_regex_identity_type(REGEX_TYPE)
    yield
    ii._REGEX_IDENTITY_TYPES.clear()
    ii._REGEX_IDENTITY_TYPES.update(saved)


def _args(type=REGEX_TYPE, value=r"[^@]+@company\.com", agent_view_code="dev", priority=None):
    return SimpleNamespace(type=type, value=value, agent_view_code=agent_view_code, priority=priority)


# ---------------------------------------------------------------------------
# _validate_regex — runs BEFORE any DB access, exits non-zero on failure
# ---------------------------------------------------------------------------

class TestValidateRegex:
    def test_valid_anchored_pattern_accepted(self):
        # no exception
        IngressBindCommand()._validate_regex(r"[^@]+@company\.com")
        IngressBindCommand()._validate_regex(r"sklep@mycompany\.com")

    def test_empty_pattern_rejected(self):
        with pytest.raises(SystemExit) as exc:
            IngressBindCommand()._validate_regex("")
        assert exc.value.code == 1

    def test_over_255_chars_rejected(self):
        with pytest.raises(SystemExit):
            IngressBindCommand()._validate_regex("a" * 256)

    def test_invalid_compile_rejected(self):
        with pytest.raises(SystemExit):
            IngressBindCommand()._validate_regex("(unclosed")

    def test_nested_quantifier_footgun_rejected(self):
        # the best-effort lint catches nested unbounded quantifiers like (a+)+
        with pytest.raises(SystemExit):
            IngressBindCommand()._validate_regex(r"(a+)+$")

    def test_alternation_overlap_NOT_caught_by_lint(self):
        # DOCUMENTS the lint's known incompleteness: (?:a|aa)+ is an alternation-overlap exponential
        # the lint cannot detect — it compiles fine and is accepted at bind time. The real runtime
        # bound is the §2 matcher `regex` timeout, exercised in test_ingress_identity.py (never here
        # via stdlib re, which would hang).
        IngressBindCommand()._validate_regex(r"(?:a|aa)+$")  # no SystemExit

    def test_rejection_does_not_echo_raw_pattern(self, capsys):
        # SEC-F3: the operator-facing message must NOT re-echo the admin's raw pattern into shared
        # logs. Use distinctive content the canned messages never contain.
        for bad in ("secretlocalpart" + "a" * 250, "(secretgroup", r"(secretname+)+"):
            with pytest.raises(SystemExit):
                IngressBindCommand()._validate_regex(bad)
            out = capsys.readouterr().out
            assert "secret" not in out


# ---------------------------------------------------------------------------
# execute — gating on is_regex_identity_type + priority pass-through
# ---------------------------------------------------------------------------

class TestExecuteGatingAndPriority:
    def _patch_db(self, monkeypatch, *, view_id=5):
        bind = MagicMock()
        get_conn = MagicMock(return_value=MagicMock())
        monkeypatch.setattr("agento.framework.cli.runtime._load_framework_config",
                            lambda: (MagicMock(), None, None))
        monkeypatch.setattr("agento.framework.db.get_connection", get_conn)
        monkeypatch.setattr("agento.framework.ingress_identity.bind_identity", bind)
        monkeypatch.setattr("agento.framework.workspace.get_agent_view_by_code",
                            lambda conn, code: SimpleNamespace(id=view_id, code=code))
        return bind, get_conn

    def test_valid_regex_binds_with_priority_none_when_omitted(self, regex_type, monkeypatch):
        bind, _ = self._patch_db(monkeypatch)
        IngressBindCommand().execute(_args(priority=None))
        assert bind.call_count == 1
        # priority preserved (None) -> bind_identity's preserve branch
        assert bind.call_args.kwargs["priority"] is None
        assert bind.call_args.args[1:4] == (REGEX_TYPE, r"[^@]+@company\.com", 5)

    def test_explicit_priority_is_passed_through(self, regex_type, monkeypatch):
        bind, _ = self._patch_db(monkeypatch)
        IngressBindCommand().execute(_args(priority=7))
        assert bind.call_args.kwargs["priority"] == 7

    def test_bad_regex_rejected_before_any_db_access(self, regex_type, monkeypatch):
        bind, get_conn = self._patch_db(monkeypatch)
        with pytest.raises(SystemExit):
            IngressBindCommand().execute(_args(value=r"(a+)+$"))
        # validation exits BEFORE opening a connection / writing anything
        get_conn.assert_not_called()
        bind.assert_not_called()

    def test_non_regex_type_skips_validation(self, regex_type, monkeypatch):
        # "jira" is NOT registered as a regex type -> validation is skipped, so a value that would
        # fail regex validation is accepted verbatim (exact identity, matched literally at runtime).
        bind, _ = self._patch_db(monkeypatch)
        IngressBindCommand().execute(_args(type="jira", value="(unclosed"))
        assert bind.call_count == 1
        assert bind.call_args.args[1] == "jira"


# ---------------------------------------------------------------------------
# Dialect parity (Rule-1 r4): CLI validator and runtime matcher use the SAME engine + VERSION0
# ---------------------------------------------------------------------------

class TestDialectParity:
    def test_cli_accepted_pattern_matches_identically_at_runtime(self, regex_type, monkeypatch):
        pattern = r"[^@]+@Company\.com"
        # 1) the CLI validator accepts it (regex.compile VERSION0|IGNORECASE, no stdlib re)
        IngressBindCommand()._validate_regex(pattern)
        # 2) the runtime matcher (regex.fullmatch VERSION0|IGNORECASE) matches case-insensitively
        row = ii.IngressIdentity(
            id=1, identity_type=REGEX_TYPE, identity_value=pattern, agent_view_id=1,
            priority=0, is_active=True,
            created_at=None, updated_at=None,
        )
        monkeypatch.setattr(ii, "get_active_identities_for_type", lambda conn, t: [row])
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "user@company.com")   # IGNORECASE
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "USER@COMPANY.COM")

    def test_validator_uses_regex_module_not_stdlib_re(self):
        # the command validates with the `regex` engine (same as the runtime matcher), not stdlib re.
        import agento.modules.core.src.commands.ingress_bind as mod
        assert mod.regex is regex
