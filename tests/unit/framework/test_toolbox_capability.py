import hashlib
import traceback

import pytest

from agento.framework.toolbox_capability import (
    KIND_INTERNAL_REST,
    KIND_MCP_INTERACTIVE,
    KIND_MCP_JOB,
    CapabilityRevokeError,
    capability_client,
    issue_capability,
    purge_expired_capabilities,
    rest_capability,
    revoke_capability,
    revoke_job_capabilities,
    token_hash,
)


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.store.append((" ".join(sql.split()), params))

    def fetchone(self):
        return {"workspace_id": 3}


class FakeConn:
    def __init__(self):
        self.statements = []
        self.commits = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self.statements)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_token_hash_is_sha256_hex():
    assert token_hash("abc") == hashlib.sha256(b"abc").hexdigest()


def test_issue_returns_raw_token_and_stores_only_the_hash():
    conn = FakeConn()
    raw = issue_capability(conn, kind=KIND_MCP_JOB, agent_view_id=7, job_id=42, ttl_seconds=3600, allowed_transports=["http"])
    assert len(raw) >= 43
    sql, params = conn.statements[-1]
    assert sql.startswith("INSERT INTO toolbox_capability")
    assert token_hash(raw) in params
    assert raw not in params
    assert conn.commits == 1


def test_issue_with_commit_false_leaves_the_transaction_to_the_caller():
    conn = FakeConn()
    issue_capability(conn, kind=KIND_MCP_JOB, agent_view_id=7, job_id=42, ttl_seconds=60, commit=False, allowed_transports=["http"])
    assert conn.commits == 0


def test_issue_rejects_unknown_kind():
    with pytest.raises(ValueError):
        issue_capability(FakeConn(), kind="root", agent_view_id=1, ttl_seconds=60, allowed_transports=["http"])


def test_issue_rejects_non_positive_ttl():
    with pytest.raises(ValueError):
        issue_capability(FakeConn(), kind=KIND_MCP_JOB, agent_view_id=1, job_id=1, ttl_seconds=0, allowed_transports=["http"])


def test_mcp_job_requires_a_job_id():
    with pytest.raises(ValueError):
        issue_capability(FakeConn(), kind=KIND_MCP_JOB, agent_view_id=1, job_id=None, ttl_seconds=60, allowed_transports=["http"])


@pytest.mark.parametrize("kind", [KIND_MCP_JOB, KIND_MCP_INTERACTIVE, KIND_INTERNAL_REST])
def test_every_kind_requires_a_positive_agent_view_id(kind):
    for bad in (None, 0, -1):
        with pytest.raises(ValueError):
            issue_capability(FakeConn(), kind=kind, agent_view_id=bad, job_id=1, ttl_seconds=60, allowed_transports=["http"], subject_id="service:test")



def test_only_a_jobless_internal_rest_capability_may_be_viewless():
    """A viewless capability serves a default-scope config test and nothing else."""
    assert issue_capability(
        FakeConn(), kind=KIND_INTERNAL_REST, agent_view_id=None, ttl_seconds=60,
        allowed_transports=["http"], subject_id="service:test",
    )
    for kind in (KIND_MCP_JOB, KIND_MCP_INTERACTIVE):
        with pytest.raises(ValueError):
            issue_capability(FakeConn(), kind=kind, agent_view_id=None, ttl_seconds=60, allowed_transports=["http"], subject_id="service:test")



def test_issue_requires_allowed_transports():
    for bad in (None, [], ["ftp"], ["http", "http"]):
        with pytest.raises(ValueError):
            issue_capability(
                FakeConn(), kind=KIND_MCP_JOB, agent_view_id=7, job_id=1, ttl_seconds=60,
                allowed_transports=bad,
            )


def test_a_token_that_may_travel_on_sse_is_issued_at_the_sse_ttl():
    conn = FakeConn()
    issue_capability(
        conn, kind=KIND_MCP_JOB, agent_view_id=7, job_id=1, ttl_seconds=86400,
        allowed_transports=["sse", "http"],
    )
    _, params = conn.statements[-1]
    assert params[4] == 14400


def test_internal_rest_cannot_be_issued_for_sse():
    with pytest.raises(ValueError, match="sse"):
        issue_capability(
            FakeConn(), kind=KIND_INTERNAL_REST, agent_view_id=7, ttl_seconds=60,
            allowed_transports=["sse"], subject_id="service:test",
        )


def test_a_service_capability_names_its_own_subject():
    for bad in (None, "", "service:legacy-internal-rest", "jira"):
        with pytest.raises(ValueError):
            issue_capability(
                FakeConn(), kind=KIND_INTERNAL_REST, agent_view_id=7, ttl_seconds=60,
                allowed_transports=["http"], subject_id=bad,
            )


def test_the_row_takes_its_workspace_from_the_agent_view():
    conn = FakeConn()
    issue_capability(
        conn, kind=KIND_MCP_JOB, agent_view_id=7, job_id=1, ttl_seconds=60,
        allowed_transports=["http"],
    )
    assert conn.statements[0] == ("SELECT workspace_id FROM agent_view WHERE id = %s", (7,))
    _, params = conn.statements[-1]
    assert params[6:8] == ("7", 3)
    with pytest.raises(ValueError, match="workspace"):
        issue_capability(
            FakeConn(), kind=KIND_MCP_JOB, agent_view_id=7, job_id=1, ttl_seconds=60,
            allowed_transports=["http"], workspace_id=4,
        )


def test_a_missing_agent_view_is_refused():
    class NoView(FakeConn):
        def cursor(self):
            cur = FakeCursor(self.statements)
            cur.fetchone = lambda: None
            return cur

    with pytest.raises(ValueError, match="does not exist"):
        issue_capability(
            NoView(), kind=KIND_MCP_JOB, agent_view_id=7, job_id=1, ttl_seconds=60,
            allowed_transports=["http"],
        )


def test_two_issues_never_collide():
    conn = FakeConn()
    a = issue_capability(conn, kind=KIND_INTERNAL_REST, agent_view_id=1, ttl_seconds=60, allowed_transports=["http"], subject_id="service:test")
    b = issue_capability(conn, kind=KIND_INTERNAL_REST, agent_view_id=1, ttl_seconds=60, allowed_transports=["http"], subject_id="service:test")
    assert a != b


def test_revoke_job_capabilities_targets_the_job_and_can_defer_commit():
    conn = FakeConn()
    revoke_job_capabilities(conn, 42, commit=False)
    sql, params = conn.statements[0]
    assert "UPDATE toolbox_capability" in sql and "revoked_at" in sql
    assert 42 in params
    assert conn.commits == 0


def test_revoke_capability_matches_by_hash_not_raw():
    conn = FakeConn()
    revoke_capability(conn, "sekret")
    _, params = conn.statements[0]
    assert token_hash("sekret") in params
    assert "sekret" not in params


def test_purge_expired_deletes_old_rows():
    conn = FakeConn()
    purge_expired_capabilities(conn)
    sql, _ = conn.statements[0]
    assert sql.startswith("DELETE FROM toolbox_capability")


def _break_the_revoke(monkeypatch):
    """Make the revoke's OWN connection attempt fail, carrying a DSN in its message.

    The revoke opens a fresh connection (the minting one is closed by then), so the failure
    that has to stay out of the log and out of the raised error is a CONNECT failure.
    """
    def _explode(config):
        raise RuntimeError("db gone: mysql://root:hunter2@db.internal")

    monkeypatch.setattr("agento.framework.toolbox_capability.get_connection", _explode)


class TestRestCapabilityLifetime:
    """A REST caller mints a bearer, builds a client, calls, closes, revokes. Every one of
    those steps can raise, and until this contextmanager existed the revoke lived in a
    `finally` that a constructor failure jumped over and a `close()` failure aborted."""

    @staticmethod
    def _revoked(conns):
        stmts = [s for c in (conns if isinstance(conns, list) else [conns]) for s in c.statements]
        return [p for sql, p in stmts if sql.startswith("UPDATE toolbox_capability SET revoked_at")]

    @pytest.fixture
    def conns(self, monkeypatch):
        """The helper owns every connection it uses — the toolbox reads the row from another
        process, so the mint must be durable and must not commit a caller's transaction. It
        opens one connection per WRITE, never one for the duration of the block."""
        opened = []

        def _open(config):
            fake = FakeConn()
            opened.append(fake)
            return fake

        monkeypatch.setattr("agento.framework.toolbox_capability.get_connection", _open)
        return opened

    def test_no_connection_is_held_while_the_block_runs(self, conns):
        """A block can be long. Pinning a pooled connection to it — one the block never uses,
        because the token travels over HTTP to another process — starves every other caller."""
        with rest_capability(agent_view_id=3, subject_id="service:test"):
            assert len(conns) == 1
            assert conns[0].closed
        assert len(conns) == 2, "the revoke opens its own connection"
        assert all(c.closed for c in conns)

    def test_the_mint_is_committed_before_the_block_runs(self, conns):
        """The toolbox validates the token from another process on another connection, so an
        uncommitted row is an unusable capability. It also means the mint can never share a
        caller's transaction: committing there would publish that caller's pending work."""
        with rest_capability(agent_view_id=3, subject_id="service:test"):
            assert conns[0].commits == 1

    def test_the_happy_path_revokes_once(self, conns):
        with rest_capability(agent_view_id=3, subject_id="service:test") as token:
            assert token
        assert len(self._revoked(conns)) == 1

    def test_a_client_constructor_failure_still_revokes(self, conns):
        """The client is built INSIDE the block precisely so this cannot leak a live bearer."""
        with pytest.raises(ValueError, match="bad base_url"), rest_capability(agent_view_id=3, subject_id="service:test"):
            raise ValueError("bad base_url")
        assert len(self._revoked(conns)) == 1

    def test_a_close_failure_still_revokes(self, conns):
        from contextlib import closing

        class Client:
            def close(self):
                raise OSError("socket already gone")

        with (
            pytest.raises(OSError, match="socket already gone"),
            rest_capability(agent_view_id=3, subject_id="service:test") as token,
            closing(Client()),
        ):
            assert token
        assert len(self._revoked(conns)) == 1

    def test_a_revoke_failure_never_masks_the_original_error(self, conns, monkeypatch, caplog):
        """A broken connection on the way out must not replace the failure that caused it —
        the operator needs the original error, and the revoke problem as a category beside it."""
        with pytest.raises(RuntimeError, match="the real failure"), rest_capability(agent_view_id=3, subject_id="service:test"):
            _break_the_revoke(monkeypatch)
            raise RuntimeError("the real failure")
        assert "RuntimeError" in caplog.text
        assert "hunter2" not in caplog.text

    def test_a_success_path_revoke_failure_reports_the_category_not_the_dsn(
        self, conns, monkeypatch, caplog
    ):
        """Here the revoke error IS the only error, so it must surface — but the driver's own
        message carries the DSN it just used, and this exception reaches an operator terminal."""
        with pytest.raises(CapabilityRevokeError) as caught, rest_capability(agent_view_id=3, subject_id="service:test"):
            _break_the_revoke(monkeypatch)
        rendered = "".join(traceback.format_exception(caught.value))
        assert "RuntimeError" in str(caught.value)
        assert "hunter2" not in rendered
        assert "db.internal" not in rendered
        assert "hunter2" not in caplog.text


class TestCapabilityClientIsMintedPerUse:
    """The lifetime rule, enforced at the call site: a capability exists only while a bounded
    request is in flight. Onboarding waits on a human between requests and retries after a
    failure — a token minted before that wait is expired by the time it is used, and live for
    the whole wait while nothing uses it."""

    @pytest.fixture
    def conns(self, monkeypatch):
        opened = []

        def _open(config):
            fake = FakeConn()
            opened.append(fake)
            return fake

        monkeypatch.setattr("agento.framework.toolbox_capability.get_connection", _open)
        return opened

    @staticmethod
    def _minted(conns):
        return [
            p for c in conns for sql, p in c.statements
            if sql.startswith("INSERT INTO toolbox_capability")
        ]

    class Client:
        def __init__(self, token):
            self.token = token
            self.closed = False

        def close(self):
            self.closed = True

    def _opener(self, seen, **kw):
        def build(token):
            client = self.Client(token)
            seen.append(client)
            return client

        return capability_client(build, agent_view_id=7, subject_id="service:test", **kw)

    def test_nothing_is_minted_until_the_client_is_opened(self, conns):
        """Building the opener is not an authorization event — the operator may still be typing."""
        self._opener([])
        assert conns == []

    def test_each_use_mints_and_revokes_its_own_capability(self, conns):
        seen = []
        opener = self._opener(seen)
        with opener() as first:
            pass
        with opener() as second:
            pass
        assert first.token != second.token, "a second request reuses a revoked token"
        assert first.closed and second.closed
        minted = self._minted(conns)
        assert len(minted) == 2
        assert all(row[1] == KIND_INTERNAL_REST and row[2] == 7 for row in minted)
        revoked = [
            p for c in conns for sql, p in c.statements
            if sql.startswith("UPDATE toolbox_capability SET revoked_at")
        ]
        assert len(revoked) == 2

    def test_no_capability_exists_between_two_uses(self, conns):
        """The wait between two requests — a `getpass` prompt, a backoff — happens with the
        previous capability already revoked and the next one not yet issued."""
        opener = self._opener([])
        with opener():
            pass
        live = self._minted(conns)[0][0]
        revoked_hashes = [
            p[0] for c in conns for sql, p in c.statements
            if sql.startswith("UPDATE toolbox_capability SET revoked_at")
        ]
        assert live in revoked_hashes

    def test_a_failed_attempt_revokes_before_the_retry_mints(self, conns):
        """The retry shape: attempt raises, its capability dies with it, the next attempt is
        authorized by a capability of its own."""
        seen = []
        opener = self._opener(seen)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="verify failed"), opener():
                raise RuntimeError("verify failed")
        assert len(self._minted(conns)) == 2
        assert seen[0].token != seen[1].token
        assert all(c.closed for c in seen)

    def test_the_client_is_closed_when_the_block_raises(self, conns):
        seen = []
        with pytest.raises(ValueError), self._opener(seen)():
            raise ValueError("boom")
        assert seen[0].closed

    def test_the_caller_db_config_is_used_for_every_mint(self, monkeypatch):
        """Defaulting to the environment is right inside the containers and wrong wherever the
        caller was handed a different database."""
        used = []

        def _open(config):
            used.append(config)
            return FakeConn()

        monkeypatch.setattr("agento.framework.toolbox_capability.get_connection", _open)
        sentinel = object()
        with self._opener([], db_config=sentinel)():
            pass
        assert used and all(c is sentinel for c in used)
