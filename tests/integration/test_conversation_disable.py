"""The `conversation` module is disableable (MOD-2), and its dependency is declared (MOD-1).

Nothing in the framework may start depending on the module being present: the queue is the
first thing that would notice, so that is what this asserts.
"""
from __future__ import annotations

import logging

from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.consumer import Consumer
from agento.framework.dependency_resolver import get_transitive_dependents
from agento.framework.module_loader import scan_modules

from .conftest import fetch_job, insert_queued_job


def test_disabling_agent_view_also_disables_conversation():
    """`sequence: ["agent_view"]` is what makes `module:disable agent_view` reach it."""
    assert "conversation" in get_transitive_dependents("agent_view", scan_modules(CORE_MODULES_DIR))


def test_conversation_declares_no_other_dependency():
    manifest = next(m for m in scan_modules(CORE_MODULES_DIR) if m.name == "conversation")
    assert manifest.sequence == ["agent_view"]


def test_the_queue_publishes_and_consumes_a_blank_job_without_the_module(
    int_db_config, int_consumer_config, monkeypatch
):
    """With `conversation` never loaded, a blank job still goes through the queue."""
    import agento.framework.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "filter_enabled",
        lambda manifests: [m for m in manifests if m.name != "conversation"],
    )
    bootstrap_module.bootstrap()
    job_id = insert_queued_job(job_type="blank", idempotency_key="conv-disabled:1",
                               reference_id="BLANK-1")

    consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))
    job = consumer._try_dequeue()

    assert job is not None and job.id == job_id
    assert fetch_job(job_id)["status"] == "RUNNING"
