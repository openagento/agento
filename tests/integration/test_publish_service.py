"""The transaction-aware publishing service (PRD E3-E5 §4.3, §6.4.2).

`publisher.publish()` answers "did I insert it?"; a conversation needs "which job is my
message waiting on?", and it needs the `job.queued` row to commit with the insert. So this
is a second entry point over one insert, not a second insert path.
"""
from __future__ import annotations

import json

import pytest

from agento.framework.events import JobPublishedEvent
from agento.framework.job_models import AgentType, JobRequester, RequesterTrust
from agento.framework.publish_service import publish_job
from agento.framework.publisher import publish

from .conftest import _test_connection


@pytest.fixture(autouse=True)
def _clean_outbox():
    conn = _test_connection(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM job_event_outbox")
    conn.close()


def _rows(table: str, where: str = "") -> list[dict]:
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} {where} ORDER BY id")
            return list(cur.fetchall())
    finally:
        conn.close()


def _publish(config, key="conv:1", **over) -> int:
    args = dict(
        source="conversation",
        agent_type=AgentType.BLANK,
        agent_view_id=None,
        reference_id="c1:m1",
        idempotency_key=key,
        requester=JobRequester(key="user:7", email="u@example.com", trust=RequesterTrust.ACCOUNT),
        priority=50,
        config=config,
    )
    args.update(over)
    return publish_job(**args)


def test_the_job_id_comes_back(int_db_config):
    job_id = _publish(int_db_config)

    jobs = _rows("job")
    assert [j["id"] for j in jobs] == [job_id]
    assert jobs[0]["reference_id"] == "c1:m1"
    assert jobs[0]["requester_trust"] == "account"


def test_the_same_key_twice_is_one_job_and_one_outbox_row(int_db_config):
    first = _publish(int_db_config)
    second = _publish(int_db_config)

    assert second == first
    assert len(_rows("job")) == 1
    assert len(_rows("job_event_outbox")) == 1


def test_the_queued_row_commits_with_the_insert(int_db_config):
    job_id = _publish(int_db_config)

    rows = _rows("job_event_outbox")
    assert len(rows) == 1
    assert rows[0]["job_id"] == job_id
    assert rows[0]["kind"] == "job.queued"
    assert rows[0]["relayed_at"] is None
    payload = json.loads(rows[0]["payload"])
    assert payload["type"] == "blank"
    assert payload["source"] == "conversation"


def test_a_prompt_is_stored(int_db_config):
    _publish(int_db_config, prompt="hello")

    assert _rows("job")[0]["prompt"] == "hello"


def test_the_racing_loser_gets_the_winners_id(int_db_config, monkeypatch):
    """The unique key rejects the second insert; the loser re-reads instead of raising."""
    from agento.framework import publish_service

    winner = _publish(int_db_config)

    real = publish_service.existing_job_id
    calls = []

    def blind_once(cur, key):
        calls.append(key)
        return None if len(calls) == 1 else real(cur, key)

    monkeypatch.setattr(publish_service, "existing_job_id", blind_once)
    assert _publish(int_db_config) == winner
    assert len(_rows("job")) == 1
    assert len(_rows("job_event_outbox")) == 1  # the rolled-back insert took its row with it


def test_the_event_carries_the_job_id(int_db_config, monkeypatch):
    seen = []
    from agento.framework import publish_service

    class _Manager:
        def dispatch(self, name, event):
            seen.append((name, event))

    monkeypatch.setattr(publish_service, "get_event_manager", lambda: _Manager())
    job_id = _publish(int_db_config)

    assert [n for n, _ in seen] == ["job_publish_after"]
    assert seen[0][1].job_id == job_id
    assert seen[0][1].idempotency_key == "conv:1"


def test_a_duplicate_dispatches_nothing(int_db_config, monkeypatch):
    from agento.framework import publish_service

    _publish(int_db_config)
    seen = []

    class _Manager:
        def dispatch(self, name, event):
            seen.append(name)

    monkeypatch.setattr(publish_service, "get_event_manager", lambda: _Manager())
    _publish(int_db_config)

    assert seen == []


def test_an_observer_that_predates_job_id_still_runs():
    """The field is optional, so every shipped observer keeps constructing the event."""
    event = JobPublishedEvent(type="blank", source="jira")

    assert event.job_id is None


def test_publish_still_returns_bool_and_writes_no_outbox_row(int_db_config):
    """`publish()` is untouched: the outbox belongs to the service, not to every publisher."""
    assert publish(int_db_config, AgentType.BLANK, "jira", "jira:1") is True
    assert publish(int_db_config, AgentType.BLANK, "jira", "jira:1") is False
    assert _rows("job_event_outbox") == []
