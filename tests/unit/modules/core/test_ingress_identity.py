"""Tests for IngressIdentity model and DB functions."""
from datetime import datetime
from time import monotonic
from unittest.mock import MagicMock

import pytest

from agento.framework import ingress_identity as ii
from agento.framework.ingress_identity import (
    IngressIdentity,
    bind_identity,
    get_identities_for_agent_view,
    get_ingress_identity,
    is_regex_identity_type,
    list_identities,
    match_ingress_identities,
    register_regex_identity_type,
    unbind_identity,
)


def _make_row(**overrides):
    base = {
        "id": 1,
        "identity_type": "email",
        "identity_value": "user@example.com",
        "agent_view_id": 10,
        "priority": 0,
        "is_active": 1,
        "created_at": datetime(2025, 1, 1),
        "updated_at": datetime(2025, 1, 1),
    }
    base.update(overrides)
    return base


class TestIngressIdentityFromRow:
    def test_from_row_basic(self):
        row = _make_row()
        identity = IngressIdentity.from_row(row)
        assert identity.id == 1
        assert identity.identity_type == "email"
        assert identity.identity_value == "user@example.com"
        assert identity.agent_view_id == 10
        assert identity.is_active is True

    def test_from_row_reads_priority(self):
        assert IngressIdentity.from_row(_make_row(priority=7)).priority == 7
        # priority arrives as a str from ENV/DB fallbacks — coerced to int
        assert IngressIdentity.from_row(_make_row(priority="5")).priority == 5
        assert IngressIdentity.from_row(_make_row(priority=-3)).priority == -3

    def test_from_row_inactive(self):
        row = _make_row(is_active=0)
        identity = IngressIdentity.from_row(row)
        assert identity.is_active is False

    def test_from_row_different_type(self):
        row = _make_row(identity_type="teams", identity_value="team-channel-id")
        identity = IngressIdentity.from_row(row)
        assert identity.identity_type == "teams"
        assert identity.identity_value == "team-channel-id"


_SENTINEL = object()


def _mock_conn(rows=None, fetchone=_SENTINEL):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    if fetchone is not _SENTINEL:
        cursor.fetchone.return_value = fetchone
    if rows is not None:
        cursor.fetchall.return_value = rows
    return conn, cursor


class TestGetIngressIdentity:
    def test_found(self):
        row = _make_row()
        conn, cursor = _mock_conn(fetchone=row)
        identity = get_ingress_identity(conn, "email", "user@example.com")
        assert identity is not None
        assert identity.identity_type == "email"
        cursor.execute.assert_called_once()

    def test_not_found(self):
        conn, _ = _mock_conn(fetchone=None)
        identity = get_ingress_identity(conn, "email", "nobody@example.com")
        assert identity is None


class TestGetIdentitiesForAgentView:
    def test_returns_list(self):
        rows = [_make_row(id=1), _make_row(id=2, identity_value="other@example.com")]
        conn, _ = _mock_conn(rows=rows)
        identities = get_identities_for_agent_view(conn, 10)
        assert len(identities) == 2
        assert identities[0].id == 1
        assert identities[1].id == 2


class TestBindIdentity:
    def test_bind_calls_execute_and_commit(self):
        conn, cursor = _mock_conn()
        bind_identity(conn, "email", "user@example.com", 10)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()


class TestUnbindIdentity:
    def test_unbind_deleted(self):
        conn, cursor = _mock_conn()
        cursor.rowcount = 1
        result = unbind_identity(conn, "email", "user@example.com")
        assert result is True
        conn.commit.assert_called_once()

    def test_unbind_not_found(self):
        conn, cursor = _mock_conn()
        cursor.rowcount = 0
        result = unbind_identity(conn, "email", "nobody@example.com")
        assert result is False


class TestListIdentities:
    def test_list_all(self):
        rows = [_make_row(id=1), _make_row(id=2, identity_type="teams")]
        conn, cursor = _mock_conn(rows=rows)
        identities = list_identities(conn)
        assert len(identities) == 2
        # No WHERE clause when no type filter
        sql = cursor.execute.call_args[0][0]
        assert "WHERE" not in sql

    def test_list_filtered_by_type(self):
        rows = [_make_row(id=1)]
        conn, cursor = _mock_conn(rows=rows)
        identities = list_identities(conn, identity_type="email")
        assert len(identities) == 1
        sql = cursor.execute.call_args[0][0]
        assert "WHERE identity_type" in sql

    def test_list_sorts_priority_desc(self):
        conn, cursor = _mock_conn(rows=[])
        list_identities(conn)
        sql = cursor.execute.call_args[0][0]
        assert "priority DESC" in sql


class TestBindIdentityPriority:
    def test_bind_without_priority_preserves_on_upsert(self):
        # priority is None -> the preserve branch: no priority in INSERT / UPDATE SET.
        conn, cursor = _mock_conn()
        bind_identity(conn, "outlook_sender", r"a@x\.com", 10)
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        assert "priority" not in sql
        assert params == ("outlook_sender", r"a@x\.com", 10)
        conn.commit.assert_called_once()

    def test_bind_with_priority_sets_and_updates(self):
        # priority given -> included in INSERT columns AND the ON DUPLICATE KEY UPDATE set.
        conn, cursor = _mock_conn()
        bind_identity(conn, "outlook_sender", r"a@x\.com", 10, priority=5)
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        assert "priority" in sql
        assert "priority = VALUES(priority)" in sql
        assert params == ("outlook_sender", r"a@x\.com", 10, 5)

    def test_bind_priority_zero_is_not_preserve_branch(self):
        # 0 is a real value, not "omitted" — must take the set branch (priority in the SQL).
        conn, cursor = _mock_conn()
        bind_identity(conn, "outlook_sender", r"a@x\.com", 10, priority=0)
        sql = cursor.execute.call_args[0][0]
        assert "priority" in sql
        assert cursor.execute.call_args[0][1][-1] == 0


class TestGetActiveIdentitiesForType:
    def test_orders_priority_desc_then_id(self):
        conn, cursor = _mock_conn(rows=[])
        ii.get_active_identities_for_type(conn, "outlook_sender")
        sql = cursor.execute.call_args[0][0]
        assert "is_active = 1" in sql
        assert "ORDER BY priority DESC, id ASC" in sql


# ---------------------------------------------------------------------------
# match_ingress_identities — regex vs exact gating + SEC-F2 ReDoS bound
# ---------------------------------------------------------------------------

REGEX_TYPE = "outlook_sender"


@pytest.fixture
def regex_type():
    """Register REGEX_TYPE as a regex-matched identity type, restoring the module-global registry
    afterwards (the registry is shared across tests — never leak it)."""
    saved = set(ii._REGEX_IDENTITY_TYPES)
    register_regex_identity_type(REGEX_TYPE)
    yield
    ii._REGEX_IDENTITY_TYPES.clear()
    ii._REGEX_IDENTITY_TYPES.update(saved)


@pytest.fixture
def clean_warn():
    """Reset the bounded warning rate-limiter around a test (module-global)."""
    ii._warn_seen.clear()
    yield
    ii._warn_seen.clear()


def _ident(id, value, *, agent_view_id=1, priority=0, is_active=True):
    return IngressIdentity(
        id=id, identity_type=REGEX_TYPE, identity_value=value, agent_view_id=agent_view_id,
        priority=priority, is_active=is_active,
        created_at=datetime(2025, 1, 1), updated_at=datetime(2025, 1, 1),
    )


def _patch_active(monkeypatch, rows):
    monkeypatch.setattr(ii, "get_active_identities_for_type", lambda conn, t: list(rows))


class TestMatchExactTypes:
    def test_non_regex_type_returns_single_active_row(self, monkeypatch):
        # api_client / jira are NOT registered -> exact match via get_ingress_identity.
        assert not is_regex_identity_type("api_client")
        monkeypatch.setattr(ii, "get_ingress_identity", lambda conn, t, v: _ident(1, v))
        result = match_ingress_identities(MagicMock(), "api_client", "some-token")
        assert [m.id for m in result] == [1]

    def test_non_regex_type_inactive_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ii, "get_ingress_identity", lambda conn, t, v: _ident(1, v, is_active=False))
        assert match_ingress_identities(MagicMock(), "api_client", "x") == []

    def test_non_regex_type_missing_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ii, "get_ingress_identity", lambda conn, t, v: None)
        assert match_ingress_identities(MagicMock(), "jira", "jira") == []

    def test_literal_dot_is_not_a_regex_for_exact_types(self, monkeypatch):
        # For an exact type the value is looked up verbatim (the dot is literal, no regex meaning).
        captured = {}

        def _get(conn, t, v):
            captured["v"] = v
            return None

        monkeypatch.setattr(ii, "get_ingress_identity", _get)
        match_ingress_identities(MagicMock(), "api_client", "aXbYc")
        assert captured["v"] == "aXbYc"  # passed through unchanged, never compiled as a pattern


class TestMatchRegexTypes:
    def test_exact_address_pattern_matches_only_itself(self, regex_type, monkeypatch):
        _patch_active(monkeypatch, [_ident(1, r"sklep@mycompany\.com")])
        assert [m.id for m in match_ingress_identities(MagicMock(), REGEX_TYPE, "sklep@mycompany.com")] == [1]
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "other@mycompany.com") == []

    def test_domain_fallback_pattern(self, regex_type, monkeypatch):
        _patch_active(monkeypatch, [_ident(1, r"[^@]+@company\.com")])
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "anyone@company.com")
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "anyone@other.com") == []
        # the [^@]+ never crosses the @, so a subdomain address does not match
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "a@sub.company.com") == []

    def test_case_insensitive(self, regex_type, monkeypatch):
        _patch_active(monkeypatch, [_ident(1, r"sklep@mycompany\.com")])
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "SKLEP@MyCompany.COM")

    def test_fullmatch_rejects_partial(self, regex_type, monkeypatch):
        _patch_active(monkeypatch, [_ident(1, r"sklep@mycompany\.com")])
        # a prefix/suffix around the pattern must NOT match (fullmatch anchors both ends)
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "xsklep@mycompany.com") == []
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "sklep@mycompany.comX") == []

    def test_returns_all_matches_in_priority_order(self, regex_type, monkeypatch):
        # match_ingress_identities preserves the get_active_identities_for_type order (priority DESC).
        rows = [
            _ident(3, r"[^@]+@company\.com", agent_view_id=2, priority=10),
            _ident(1, r"sklep@company\.com", agent_view_id=1, priority=5),
        ]
        _patch_active(monkeypatch, rows)
        matched = match_ingress_identities(MagicMock(), REGEX_TYPE, "sklep@company.com")
        assert [m.id for m in matched] == [3, 1]  # both match, order preserved

    def test_negative_priority_binding_still_matches(self, regex_type, monkeypatch):
        _patch_active(monkeypatch, [_ident(1, r"a@x\.com", priority=-5)])
        assert [m.id for m in match_ingress_identities(MagicMock(), REGEX_TYPE, "a@x.com")] == [1]

    def test_is_regex_identity_type_drives_branch(self, regex_type, monkeypatch):
        assert is_regex_identity_type(REGEX_TYPE)
        # exact-type helper must NOT be consulted for a regex type
        monkeypatch.setattr(ii, "get_ingress_identity",
                            lambda *a: pytest.fail("regex type must not use get_ingress_identity"))
        _patch_active(monkeypatch, [_ident(1, r"a@x\.com")])
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "a@x.com")

    def test_sender_over_320_chars_returns_empty(self, regex_type, monkeypatch):
        # a sender longer than the RFC 5321 cap is skipped without even querying the bindings
        monkeypatch.setattr(ii, "get_active_identities_for_type",
                            lambda conn, t: pytest.fail("should not query for an over-long sender"))
        assert match_ingress_identities(MagicMock(), REGEX_TYPE, "a" * 321) == []


class TestMatchRedosBound:
    # (?:a|aa)+$ is an alternation-overlap exponential that DOES catastrophically backtrack in the
    # `regex` engine (unlike (a+)+$, which it optimizes) — the CLI lint cannot catch it, so the
    # runtime timeout is the real bound. Safe to exercise here ONLY because regex.fullmatch has a
    # timeout; stdlib re would hang.
    PATHOLOGICAL = r"(?:a|aa)+$"
    ADVERSARIAL = "a" * 60 + "!"

    def test_pathological_pattern_skipped_within_budget(self, regex_type, clean_warn, monkeypatch, caplog):
        _patch_active(monkeypatch, [_ident(7, self.PATHOLOGICAL)])
        import logging
        t0 = monotonic()
        with caplog.at_level(logging.WARNING, logger="agento.framework.ingress_identity"):
            result = match_ingress_identities(MagicMock(), REGEX_TYPE, self.ADVERSARIAL)
        elapsed = monotonic() - t0
        assert result == []                    # timed out -> skipped -> deterministic no-match
        assert elapsed < 0.4                   # bounded by the per-pattern budget (~0.1s) + slack
        msgs = [r.getMessage() for r in caplog.records]
        assert any("id=7" in m for m in msgs)          # WARN references the binding id
        assert all(self.PATHOLOGICAL not in m for m in msgs)  # never the raw pattern (SEC-F3)

    def test_whole_lookup_bounded_by_total_deadline(self, regex_type, clean_warn, monkeypatch):
        # B pathological bindings: total time is bounded by the per-lookup deadline (~0.5s),
        # NOT B x per-pattern (20 x 0.1 = 2.0s). Remaining rows fail closed after the budget.
        rows = [_ident(100 + i, self.PATHOLOGICAL) for i in range(20)]
        _patch_active(monkeypatch, rows)
        t0 = monotonic()
        result = match_ingress_identities(MagicMock(), REGEX_TYPE, self.ADVERSARIAL)
        elapsed = monotonic() - t0
        assert result == []
        assert elapsed < 1.5                   # << 20 x 0.1s == 2.0s -> total-deadline bounded

    def test_invalid_stored_regex_skipped_valid_still_matched(self, regex_type, clean_warn, monkeypatch, caplog):
        import logging
        rows = [_ident(5, "(unclosed"), _ident(6, r"good@x\.com")]
        _patch_active(monkeypatch, rows)
        with caplog.at_level(logging.WARNING, logger="agento.framework.ingress_identity"):
            result = match_ingress_identities(MagicMock(), REGEX_TYPE, "good@x.com")
        assert [m.id for m in result] == [6]   # invalid skipped, valid matched — never raised
        assert any("id=5" in r.getMessage() for r in caplog.records)

    def test_warning_rate_limited_per_binding_across_senders(self, regex_type, clean_warn, monkeypatch, caplog):
        import logging
        _patch_active(monkeypatch, [_ident(42, self.PATHOLOGICAL)])
        with caplog.at_level(logging.WARNING, logger="agento.framework.ingress_identity"):
            for i in range(3):  # 3 lookups, 3 distinct senders, same pathological binding
                match_ingress_identities(MagicMock(), REGEX_TYPE, "a" * (50 + i) + "!")
        skip_warnings = [r for r in caplog.records if "id=42" in r.getMessage()]
        assert len(skip_warnings) == 1         # emitted at most once per binding id per window

    def test_warn_limiter_is_capped(self, clean_warn):
        for i in range(ii._WARN_LRU_MAX + 50):
            ii._should_warn(i)
        assert len(ii._warn_seen) <= ii._WARN_LRU_MAX  # bounded LRU — cannot grow without bound
