"""Integration: the GitHub publish pipeline end-to-end against real MySQL + real scoped config.

The unit tests mock the toolbox client + framework publish; this closes the gap by running run_lane
against a real ``agent_view`` + ``core_config_data`` + ``job`` table, with only the toolbox HTTP
(``/api/github/open-prs``) stubbed via respx. It proves the controlling ACC: right job rows per lane,
the merged three-surface watermark, idempotency, the fail-closed ENV guard in FRONT of toolbox
contact, and the two cross-language guards (ENV key parity, rate-limit hold vs HTTP timeout).
"""
from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path

import pytest
import respx
from httpx import Response

from agento.framework.scoped_config import Scope, scoped_config_set
from agento.modules.github.src import env_guard
from agento.modules.github.src.commands._loop import run_lane
from agento.modules.github.src.review_scan import build_comments_key
from agento.modules.github.src.toolbox_client import GitHubToolboxClient

from .conftest import _test_connection, fetch_all_jobs

TOOLBOX_URL = "http://toolbox:3001"
AGENT = "agent-bot"
REV = "reviewer-jane"
T1 = "2026-01-01T10:00:00Z"
T2 = "2026-01-01T11:00:00Z"
T3 = "2026-01-01T12:00:00Z"
T4 = "2026-01-01T13:00:00Z"

MODULE_DIR = Path(env_guard.__file__).resolve().parent.parent
JS_ENV_GUARD = MODULE_DIR / "toolbox" / "env-guard.js"
JS_AUTH = MODULE_DIR / "toolbox" / "github-auth.js"

logger = logging.getLogger("it-github")


def _set_cfg(cur_conn, scope, scope_id, **values):
    for key, val in values.items():
        scoped_config_set(cur_conn, f"github/{key}", val, scope=scope, scope_id=scope_id)


@pytest.fixture
def github_env():
    """Create workspaces/agent_views + scoped github config; clean up (cascade + config rows)."""
    conn = _test_connection(autocommit=True)
    created = {}

    def make_view(ws_code, av_code, *, scope=Scope.AGENT_VIEW, **cfg):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO workspace (code, label) VALUES (%s, %s)", (ws_code, ws_code))
            ws_id = cur.lastrowid
            cur.execute(
                "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                (ws_id, av_code, av_code),
            )
            av_id = cur.lastrowid
        scope_id = av_id if scope == Scope.AGENT_VIEW else 0
        _set_cfg(conn, scope, scope_id, **cfg)
        created.setdefault("ws", []).append(ws_code)
        return av_id

    try:
        yield make_view
    finally:
        with conn.cursor() as cur:
            for ws_code in created.get("ws", []):
                cur.execute("DELETE FROM workspace WHERE code = %s", (ws_code,))
            cur.execute("DELETE FROM core_config_data WHERE path LIKE 'github/%%'")
        conn.close()


def _enabled_view(github_env, ws, av, **overrides):
    cfg = {
        "enabled": "1",
        "github_owner": "acme",
        "github_login": AGENT,
        "repo_allowlist": "api",
    }
    cfg.update(overrides)
    return github_env(ws, av, **cfg)


def _comment(cid, *, login=REV, created_at=T3, surface="review", resolved=False):
    return {
        "id": cid, "author_login": login, "created_at": created_at,
        "surface": surface, "resolved": resolved,
    }


def _comments_pr(comments=None, *, commits=(T1,), truncated=None):
    pr = {
        "owner": "acme", "repo": "api", "id": 7, "title": "Add X", "updated_at": T3,
        "comments": list(comments) if comments is not None else [_comment(501)],
        "commits": [{"date": d} for d in commits],
    }
    if truncated:
        pr["truncated"] = list(truncated)
    return pr


def _review(rid, *, login=REV, date=T2, state="CHANGES_REQUESTED"):
    return {"id": rid, "user_login": login, "date": date, "state": state}


def _changes_pr(reviews=None):
    return {
        "owner": "acme", "repo": "api", "id": 7, "title": "Add X", "updated_at": T3,
        "reviews": list(reviews) if reviews is not None else [_review(900)],
    }


def _comments_key(created_at, identities):
    """Rebuild the comments-lane key the way the module does — timestamp + digest of the whole
    same-second identity set (see review_scan.build_comments_key)."""
    return build_comments_key("acme/api:7", created_at, identities)


def _mock_open_prs(pull_requests, *, errors=None, status=200):
    return respx.post(f"{TOOLBOX_URL}/api/github/open-prs").mock(
        return_value=Response(
            status,
            json={"pull_requests": pull_requests, "errors": list(errors or [])}
            if status == 200 else {"detail": "boom"},
        )
    )


# --------------------------------------------------------------------------- #
# 1 / 2 / 2b — the two lanes

@respx.mock
def test_comments_lane_writes_job_with_requester(int_db_config, github_env):
    _enabled_view(github_env, "ws-gh-1", "av-gh-1")
    _mock_open_prs([_comments_pr()])
    conn = _test_connection(autocommit=True)
    try:
        published = run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments")
    finally:
        conn.close()
    assert published == 1
    jobs = fetch_all_jobs()
    assert len(jobs) == 1
    row = jobs[0]
    assert row["source"] == "github-comments"
    assert row["reference_id"] == "acme/api:7"
    assert row["idempotency_key"] == _comments_key(T3, [("review", 501)])
    assert row["requester_key"] == f"github:login:{REV}"
    assert row["requester_trust"] == "account"
    assert row["priority"] == 50


@respx.mock
def test_changes_lane_writes_prioritized_job(int_db_config, github_env):
    _enabled_view(github_env, "ws-gh-2", "av-gh-2")
    _mock_open_prs([_changes_pr()])
    conn = _test_connection(autocommit=True)
    try:
        published = run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="changes")
    finally:
        conn.close()
    assert published == 1
    jobs = fetch_all_jobs()
    assert len(jobs) == 1
    assert jobs[0]["source"] == "github-changes"
    assert jobs[0]["idempotency_key"] == f"github:changes:acme/api:7:{T2}:900"
    assert jobs[0]["priority"] == 80  # base 50 + bump 30


@respx.mock
def test_changes_lane_ignores_a_superseded_request(int_db_config, github_env):
    # G-16: GitHub keeps the CHANGES_REQUESTED review forever; the reviewer's LATEST deciding review
    # is their current position, and here it is an approval ⇒ no outstanding work.
    _enabled_view(github_env, "ws-gh-3", "av-gh-3")
    _mock_open_prs([_changes_pr([_review(900), _review(901, date=T3, state="APPROVED")])])
    conn = _test_connection(autocommit=True)
    try:
        published = run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="changes")
    finally:
        conn.close()
    assert published == 0
    assert len(fetch_all_jobs()) == 0


# --------------------------------------------------------------------------- #
# 3 / 4 — idempotency

@respx.mock
def test_rerun_identical_data_queues_nothing(int_db_config, github_env):
    _enabled_view(github_env, "ws-gh-4", "av-gh-4")
    _mock_open_prs([_comments_pr()])
    conn = _test_connection(autocommit=True)
    try:
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 1
        # Identical data ⇒ identical idempotency key + the job is still active ⇒ nothing new.
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 0
    finally:
        conn.close()
    assert len(fetch_all_jobs()) == 1


@respx.mock
def test_new_feedback_after_completion_queues_new_job(int_db_config, github_env):
    _enabled_view(github_env, "ws-gh-5", "av-gh-5")
    route = _mock_open_prs([_comments_pr()])
    conn = _test_connection(autocommit=True)
    try:
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 1
        with conn.cursor() as cur:
            cur.execute("UPDATE job SET status = 'SUCCESS' WHERE source = 'github-comments'")
        # A genuinely newer comment ⇒ a new idempotency key ⇒ a new job.
        route.mock(return_value=Response(200, json={
            "pull_requests": [_comments_pr([_comment(501), _comment(502, created_at=T4)])],
            "errors": [],
        }))
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 1
    finally:
        conn.close()
    keys = sorted(j["idempotency_key"] for j in fetch_all_jobs())
    assert keys == sorted([
        _comments_key(T3, [("review", 501)]),
        _comments_key(T4, [("review", 502)]),
    ])


@respx.mock
def test_a_same_second_sibling_on_another_surface_queues_a_new_job(int_db_config, github_env):
    """Regression: the key folds in EVERY identity at the newest second, so a comment posted in the
    same second as the published one — with a lower id, from a different surface's id namespace —
    cannot be silently absorbed by the existing job."""
    _enabled_view(github_env, "ws-gh-5b", "av-gh-5b")
    route = _mock_open_prs([_comments_pr([_comment(501, surface="review")])])
    conn = _test_connection(autocommit=True)
    try:
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 1
        with conn.cursor() as cur:
            cur.execute("UPDATE job SET status = 'SUCCESS' WHERE source = 'github-comments'")
        route.mock(return_value=Response(200, json={
            "pull_requests": [_comments_pr([
                _comment(501, surface="review"), _comment(7, surface="issue"),  # same T3, lower id
            ])],
            "errors": [],
        }))
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 1
    finally:
        conn.close()
    keys = sorted(j["idempotency_key"] for j in fetch_all_jobs())
    assert keys == sorted([
        _comments_key(T3, [("review", 501)]),
        _comments_key(T3, [("review", 501), ("issue", 7)]),
    ])


# --------------------------------------------------------------------------- #
# 5 / 6 / 7 — the comments-lane silencing rules

@respx.mock
def test_resolved_thread_produces_no_job(int_db_config, github_env):
    _enabled_view(github_env, "ws-gh-6", "av-gh-6")
    _mock_open_prs([_comments_pr([_comment(501, resolved=True)])])
    conn = _test_connection(autocommit=True)
    try:
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 0
    finally:
        conn.close()
    assert len(fetch_all_jobs()) == 0


@respx.mock
def test_feedback_in_the_same_second_as_the_agents_reply_still_queues(int_db_config, github_env):
    # Second-precision timestamps: the reviewer's follow-up shares the agent's reply second. The strict
    # watermark comparison dropped it silently and forever — end to end, not just in the pure function.
    _enabled_view(github_env, "ws-gh-7b", "av-gh-7b")
    _mock_open_prs([_comments_pr([
        _comment(900, login=AGENT, created_at=T3, surface="issue"),
        _comment(501, created_at=T3, surface="review"),
    ])])
    conn = _test_connection(autocommit=True)
    try:
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 1
    finally:
        conn.close()
    jobs = fetch_all_jobs()
    assert len(jobs) == 1
    assert jobs[0]["idempotency_key"] == _comments_key(T3, [("review", 501)])


@respx.mock
def test_feedback_in_the_head_commits_own_second_still_queues(int_db_config, github_env):
    # The same tie against the force-push watermark: sharing the commit's second is not evidence that
    # the push answered the comment.
    _enabled_view(github_env, "ws-gh-7c", "av-gh-7c")
    _mock_open_prs([_comments_pr([_comment(501, created_at=T4)], commits=(T4,))])
    conn = _test_connection(autocommit=True)
    try:
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 1
    finally:
        conn.close()
    assert len(fetch_all_jobs()) == 1


@respx.mock
def test_force_push_after_feedback_produces_no_job(int_db_config, github_env):
    # The watermark is a timestamp, not an id: a commit newer than the newest comment answers it.
    _enabled_view(github_env, "ws-gh-7", "av-gh-7")
    _mock_open_prs([_comments_pr([_comment(501, created_at=T3)], commits=(T4,))])
    conn = _test_connection(autocommit=True)
    try:
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 0
    finally:
        conn.close()
    assert len(fetch_all_jobs()) == 0


@respx.mock
def test_agent_reply_on_another_surface_silences_the_comment(int_db_config, github_env):
    # Three-surface merge: the reviewer's ISSUE comment is answered by the agent's REVIEW BODY.
    _enabled_view(github_env, "ws-gh-8", "av-gh-8")
    _mock_open_prs([_comments_pr([
        _comment(501, created_at=T2, surface="issue"),
        _comment(900, login=AGENT, created_at=T3, surface="review_body"),
    ])])
    conn = _test_connection(autocommit=True)
    try:
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 0
    finally:
        conn.close()
    assert len(fetch_all_jobs()) == 0


# --------------------------------------------------------------------------- #
# 8 / 9 / 10 — enablement + failure isolation

@respx.mock
def test_disabled_view_never_calls_the_toolbox(int_db_config, github_env):
    _enabled_view(github_env, "ws-gh-9", "av-gh-9", enabled="0")
    route = _mock_open_prs([_comments_pr()])
    conn = _test_connection(autocommit=True)
    try:
        assert run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments") == 0
    finally:
        conn.close()
    assert len(fetch_all_jobs()) == 0
    assert not route.called


@respx.mock
def test_toolbox_failure_is_isolated(int_db_config, github_env, caplog):
    _enabled_view(github_env, "ws-gh-10", "av-gh-10")
    _mock_open_prs([], status=500)
    conn = _test_connection(autocommit=True)
    try:
        with caplog.at_level(logging.ERROR, logger="it-github"):
            published = run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments")
    finally:
        conn.close()
    assert published == 0
    assert len(fetch_all_jobs()) == 0
    assert "failed for view av-gh-10" in caplog.text


@respx.mock
def test_repo_errors_do_not_block_the_prs_that_were_returned(int_db_config, github_env, caplog):
    _enabled_view(github_env, "ws-gh-11", "av-gh-11", repo_allowlist="api,web")
    _mock_open_prs([_comments_pr()], errors=[{"repo": "web", "error": "HTTP 404"}])
    conn = _test_connection(autocommit=True)
    try:
        with caplog.at_level(logging.WARNING, logger="it-github"):
            published = run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments")
    finally:
        conn.close()
    assert published == 1
    assert len(fetch_all_jobs()) == 1
    assert "HTTP 404" in caplog.text


# --------------------------------------------------------------------------- #
# 11 — the ENV guard sits in FRONT of toolbox contact

@respx.mock
@pytest.mark.parametrize("key", env_guard.VIEW_SCOPED_ENV_KEYS)
def test_global_env_override_stops_the_pipeline_before_any_toolbox_call(
    int_db_config, github_env, caplog, monkeypatch, key,
):
    _enabled_view(github_env, f"ws-gh-12-{key[-4:].lower()}", f"av-gh-12-{key[-4:].lower()}")
    route = _mock_open_prs([_comments_pr()])
    monkeypatch.setenv(key, "x")
    conn = _test_connection(autocommit=True)
    try:
        with caplog.at_level(logging.ERROR, logger="it-github"):
            published = run_lane(int_db_config, conn, TOOLBOX_URL, logger, lane="comments")
    finally:
        conn.close()
    assert published == 0
    assert len(fetch_all_jobs()) == 0
    assert key in caplog.text
    assert not route.called  # the guard is in front of the toolbox, not behind it


# --------------------------------------------------------------------------- #
# 12 / 13 — the cross-language guards

def test_the_two_env_key_lists_cannot_drift():
    """The Python and JS halves of the two-sided guard must defend the SAME set of fields.

    Structural, not a word search: it compares the enforced key sets. Adding a fourth view-scoped
    field to one language and not the other fails here.
    """
    js_keys = set(re.findall(r"CONFIG__GITHUB__[A-Z_]+", JS_ENV_GUARD.read_text()))
    assert js_keys == set(env_guard.VIEW_SCOPED_ENV_KEYS)


def test_rate_limit_hold_cannot_outlive_the_publisher_timeout():
    """The toolbox's rate-limit hold plus a request's worth of slack must fit inside the publisher's
    HTTP timeout — otherwise the poll times out mid-hold and the wait buys nothing. Both numbers are
    read from their real sources, so changing either side alone fails here."""
    timeout_default = inspect.signature(GitHubToolboxClient.__init__).parameters["timeout"].default
    match = re.search(r"RATE_LIMIT_MAX_WAIT_MS = (\d+)", JS_AUTH.read_text())
    assert match, "RATE_LIMIT_MAX_WAIT_MS not found in github-auth.js"
    max_wait_ms = int(match.group(1))
    assert max_wait_ms / 1000 + 10 <= timeout_default
