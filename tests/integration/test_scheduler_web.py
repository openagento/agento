"""The scheduler in ``web``, which never runs ``bootstrap()``: what ``load_module_routes``
registers must be enough for the panel routes (AG-67 plan §9)."""
from __future__ import annotations

import pytest

from agento.framework import bootstrap, job_types
from agento.framework.access import accounts
from agento.framework.channels import registry as channels
from agento.framework.module_discovery import module_dirs_by_name
from agento.framework.schedule import sources
from agento.modules.scheduler.src import routes, store
from agento.modules.scheduler.src.channel import SchedulerChannel
from agento.modules.scheduler.src.source import DELETED_SUFFIX, SchedulerSource
from agento.web.routes_registry import load_module_routes

from .test_conversation_submission import _Req


@pytest.fixture
def web(monkeypatch, tmp_path):
    """A fresh process as web starts it: no manifests, no channel, no module job type."""
    monkeypatch.setattr(bootstrap, "_MANIFESTS", [])
    monkeypatch.setattr(channels, "_CHANNELS", {})
    monkeypatch.setattr(job_types, "_REGISTRY", {})
    monkeypatch.setattr(sources, "_REGISTERED", {})
    (tmp_path / "etc").mkdir()
    (tmp_path / "code").mkdir()
    return load_module_routes(tmp_path / "etc", module_dirs_by_name(bootstrap.CORE_MODULES_DIR,
                                                                    str(tmp_path / "code")))


def test_web_registers_the_routes_the_channel_the_job_type_and_the_source(web):
    assert isinstance(sources.get_source("scheduler"), SchedulerSource)
    assert any(r.pattern.match("/api/scheduler/schedules") for r in web)
    assert isinstance(channels.get_channel("scheduler"), SchedulerChannel)
    assert job_types.resolve_job_type("scheduler").value == "scheduler"


def test_run_now_works_in_web(conn, world, web):
    accounts.add_grant(conn, "user", "operation", "scheduler.own")
    accounts.add_grant(conn, "user", "operation", "scheduler.own.write")
    created = routes.create(_Req(conn, world["owner"], json={
        "title": "t", "prompt": "p", "agent_view_id": world["view"], "rrule": "FREQ=HOURLY;BYMINUTE=0"}))
    assert created.status == 201
    ran = routes.run_now(_Req(conn, world["owner"], params={"id": str(created.body["id"])}))
    assert ran.status == 201, ran.body


def test_a_panel_delete_in_web_retitles_the_conversation(conn, world, web, monkeypatch):
    accounts.add_grant(conn, "user", "operation", "scheduler.own")
    accounts.add_grant(conn, "user", "operation", "scheduler.own.share")
    sid = routes.create(_Req(conn, world["owner"], json={
        "title": "t", "prompt": "p", "agent_view_id": world["view"], "rrule": "FREQ=HOURLY;BYMINUTE=0"})).body["id"]
    with monkeypatch.context() as m:  # a run made by the tick, where the source is registered
        m.setattr(sources, "load_sources", lambda: {"scheduler": SchedulerSource()})
        _, _, conv_id = store.run_now(conn, sid, user=world["admin"], actor="test")
    assert routes.delete(_Req(conn, world["owner"], params={"id": str(sid)})).status == 204
    with conn.cursor() as cur:
        cur.execute("SELECT title FROM conversation WHERE id = %s", (conv_id,))
        assert cur.fetchone()["title"] == "t" + DELETED_SUFFIX
