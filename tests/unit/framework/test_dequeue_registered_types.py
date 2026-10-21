"""Plan §5.7: the ordinary dequeue selects only job types that are registered now.

A TODO row of an unregistered type (its module disabled) must not stall the queue head.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.consumer import Consumer
from agento.framework.job_types import (
    BUILTIN_JOB_TYPES,
    clear_job_types,
    register_job_type,
    registered_job_types,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_job_types()
    yield
    clear_job_types()


def test_registered_job_types_follow_the_registry():
    assert registered_job_types() == BUILTIN_JOB_TYPES

    register_job_type("subagent", module="subagent")
    assert registered_job_types() == (*BUILTIN_JOB_TYPES, "subagent")

    clear_job_types()
    assert registered_job_types() == BUILTIN_JOB_TYPES


def _dequeue_params(sample_db_config, sample_consumer_config):
    conn, cur = MagicMock(), MagicMock()
    cur.fetchone.return_value = None
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    with patch("agento.framework.consumer.get_connection", return_value=conn):
        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        assert consumer._try_dequeue() is None
    [(sql, params)] = [c.args for c in cur.execute.call_args_list
                       if "scheduled_after <= NOW()" in c.args[0]]
    assert sql.count("%s") == len(params)
    assert "type IN (" in sql
    return params


def test_dequeue_filters_on_the_types_registered_at_each_poll(sample_db_config, sample_consumer_config):
    register_job_type("subagent", module="subagent")
    assert _dequeue_params(sample_db_config, sample_consumer_config) == (*BUILTIN_JOB_TYPES, "subagent")

    clear_job_types()  # module disabled on hot-reload: its rows are no longer candidates
    assert _dequeue_params(sample_db_config, sample_consumer_config) == BUILTIN_JOB_TYPES
