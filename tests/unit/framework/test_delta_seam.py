"""The delta seam's boundaries (PRD E3-E5 §6.4.1, §8.2, PLC-2, MOD-2).

Not the queue mechanics (tests/unit/framework/test_execution_deltas.py) and not the rows
(tests/integration/test_delta_sink.py). What is asserted here is where the line between the
framework and the module falls, and that a run with no sink and no mapper is an ordinary run.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from agento.framework import consumer, execution_deltas
from agento.framework.harness.protocols import StreamEventMapper
from agento.framework.runner.server import map_line

FRAMEWORK = Path("src/agento/framework")


@pytest.fixture(autouse=True)
def _reset():
    execution_deltas._reset_for_tests()
    yield
    execution_deltas._reset_for_tests()


class _Adapter:
    def __init__(self, mapper=None) -> None:
        if mapper is not None:
            self.stream_event_mapper = mapper


class _Entry:
    def __init__(self, adapter) -> None:
        self.adapter = adapter


class _Mapper:
    def __init__(self, result=None, boom=False) -> None:
        self.result, self.boom, self.seen = result, boom, []

    def map_event(self, event: dict):
        self.seen.append(event)
        if self.boom:
            raise RuntimeError("the mapper is broken")
        return self.result


class _Sink:
    def __init__(self) -> None:
        self.batches = []

    def write(self, batch) -> None:
        self.batches.append(list(batch))


def _per_line(mapper, on_fragment):
    """Both halves of the seam: the runner maps a line, the worker submits (WS5)."""
    if on_fragment is None:
        return None
    return lambda line: [on_fragment(f) for f in map_line(mapper, line)]


def _callback(*, sink=True, mapper=_Mapper, execution_id="e1"):
    if sink:
        execution_deltas.sync(_Sink(), module="conversation", class_path="src.deltas.Sink")
    instance = mapper() if isinstance(mapper, type) else mapper
    import logging
    return _per_line(instance, consumer._delta_callback(_Entry(_Adapter(instance)), execution_id,
                                                        logging.getLogger("t"))), instance


# --- the three conditions, each on its own ---------------------------------

def test_with_no_sink_registered_nothing_is_attached(monkeypatch):
    """A disabled module attaches no callback at all - the harness is not even read."""
    callback, _ = _callback(sink=False)

    assert callback is None


def test_with_no_execution_id_nothing_is_attached():
    callback, _ = _callback(execution_id=None)

    assert callback is None


def test_a_harness_with_no_mapper_streams_nothing_extra():
    """The mapper is optional: a harness that declares none is complete, not degraded."""
    execution_deltas.sync(_Sink(), module="conversation", class_path="src.deltas.Sink")
    import logging

    assert consumer._delta_callback(_Entry(_Adapter()), "e1", logging.getLogger("t")) is None


def test_all_three_present_attaches_a_callback():
    callback, _ = _callback(mapper=_Mapper({"kind": "delta", "text": "hi"}))

    assert callable(callback)


# --- what the callback does ------------------------------------------------

def test_the_callback_enqueues_and_does_no_database_work(monkeypatch):
    """It runs on the harness's stdout drain thread; a query here stalls the run."""
    import agento.framework.db as db

    monkeypatch.setattr(db, "get_connection", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the delta callback opened a connection")))
    callback, _ = _callback(mapper=_Mapper({"kind": "delta", "text": "hi"}))

    callback('{"type": "text"}\n')

    assert execution_deltas._worker.pending() >= 0      # it returned; nothing queried


def test_a_mapper_that_returns_none_suppresses_the_event():
    callback, mapper = _callback(mapper=_Mapper(None))

    callback('{"type": "init"}')

    assert mapper.seen == [{"type": "init"}]


def test_a_line_that_is_not_json_is_ignored():
    callback, mapper = _callback(mapper=_Mapper({"kind": "delta", "text": "x"}))

    callback("not json at all\n")

    assert mapper.seen == []


def test_a_json_line_that_is_not_an_object_is_ignored():
    callback, mapper = _callback(mapper=_Mapper({"kind": "delta", "text": "x"}))

    callback("[1, 2, 3]")

    assert mapper.seen == []


def test_a_mapper_that_raises_costs_a_delta_and_never_the_run():
    callback, _ = _callback(mapper=_Mapper(boom=True))

    callback('{"type": "text"}')      # must not raise


def test_the_sequence_is_per_run_and_increases():
    sink = _Sink()
    execution_deltas.sync(sink, module="conversation", class_path="src.deltas.Sink")
    import logging
    mapper = _Mapper({"kind": "delta", "text": "x"})
    callback = _per_line(mapper, consumer._delta_callback(
        _Entry(_Adapter(mapper)), "e1", logging.getLogger("t")))
    worker = execution_deltas._worker
    worker.stop()                      # hold the batch in the queue to read it
    worker.thread.join(2)

    for _ in range(3):
        callback('{"type": "text"}')

    assert [r.seq for r in worker._queue] == [1, 2, 3]


# --- the framework/module boundary (PLC-2, §6.4.1) -------------------------

def test_no_framework_file_writes_a_module_table():
    """§14 puts the queue and the thread in the framework; §6.4.1 says the framework writes
    no module table. Both hold only because the thread owns the mechanics and not the rows."""
    hits = subprocess.run(
        ["grep", "-rnE", r"(INSERT|UPDATE|DELETE)[^\n]*(execution_delta|conversation_event)",
         str(FRAMEWORK)],
        capture_output=True, text=True).stdout.strip()

    assert hits == "", hits


def test_the_delta_mechanics_module_contains_no_sql_at_all():
    source = (FRAMEWORK / "execution_deltas.py").read_text().upper()

    for verb in ("INSERT ", "UPDATE ", "DELETE ", "SELECT "):
        assert verb not in source, verb


def test_the_framework_delta_path_names_no_vendor():
    """PLC-2: `kind` is the framework's vocabulary, so a reader never has to know which CLI
    produced the run. An AST check on string literals, not a text search - prose about a
    harness is legitimate and a word ban fires on it."""
    vendors = {"claude", "codex", "anthropic", "openai", "pi"}
    tree = ast.parse((FRAMEWORK / "execution_deltas.py").read_text())
    literals = {n.value.strip("\"'").lower() for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    assert not (literals & vendors)


def test_the_mapper_protocol_is_structural_and_optional():
    """Read with `getattr`, never declared on `AgentHarnessAdapter`: that protocol is
    runtime_checkable, so a declared member would stop every existing harness loading."""
    from agento.framework.harness.protocols import AgentHarnessAdapter

    assert isinstance(_Mapper(), StreamEventMapper)
    assert "stream_event_mapper" not in AgentHarnessAdapter.__protocol_attrs__


def test_the_consumer_stops_the_delta_worker_on_its_way_out():
    """CODE-4: `execution_deltas.shutdown()` had no production caller, so the daemon thread
    outlived the module it writes for. A call-graph check, not a text search: what matters is
    that the consumer's own shutdown path is what calls it, and before `dispatch_shutdown`.
    """
    tree = ast.parse((FRAMEWORK / "consumer.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "shutdown"
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "execution_deltas"]
    plain = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "dispatch_shutdown"]

    assert len(calls) == 1
    assert calls[0].lineno < plain[0].lineno       # drain while the sink is still live


# --- the canonical fragment vocabulary (E9 §3.2) ---------------------------

def _held(mapper, *, secrets=()):
    """A callback whose queue is held, so the records it enqueues can be read."""
    import logging
    execution_deltas.sync(_Sink(), module="conversation", class_path="src.deltas.Sink")
    callback = _per_line(mapper, consumer._delta_callback(_Entry(_Adapter(mapper)), "e1",
                                                          logging.getLogger("t"), secrets))
    worker = execution_deltas._worker
    worker.stop()
    worker.thread.join(2)
    return callback, worker._queue


def test_a_mapper_may_return_several_fragments_for_one_event():
    callback, queue = _held(_Mapper([
        {"kind": "assistant.text", "text": "hi"},
        {"kind": "tool.started", "tool_name": "Read", "data": {"call_id": "c1", "input": "{}"}},
    ]))

    callback('{"type": "assistant"}')

    assert [(r.seq, r.kind) for r in queue] == [(1, "assistant.text"), (2, "tool.started")]
    assert queue[1].data == {"call_id": "c1", "input": "{}"}


@pytest.mark.parametrize("fragment", [{"kind": "delta", "text": "hi"}, {"text": "hi"}])
def test_the_pre_e9_fragment_shape_still_means_assistant_text(fragment):
    """CODE-5: an out-of-tree mapper written to the old docstring keeps working."""
    callback, queue = _held(_Mapper(fragment))

    callback('{"type": "text"}')

    assert [(r.kind, r.text) for r in queue] == [("assistant.text", "hi")]


def test_an_unknown_kind_is_dropped():
    callback, queue = _held(_Mapper([{"kind": "thinking", "text": "x"},
                                     {"kind": "error", "text": "boom"}]))

    callback('{"type": "x"}')

    assert [r.kind for r in queue] == ["error"]


def test_both_capability_tokens_are_redacted_from_text_and_data():
    """The stream must not be a way around the redaction `job.output` gets (SEC-6)."""
    callback, queue = _held(_Mapper({
        "kind": "tool.completed", "text": "t TOKMCP",
        "data": {"call_id": "c1", "output": "TOKREST and TOKMCP", "is_error": False}}),
        secrets=("TOKMCP", "TOKREST"))

    callback('{"type": "x"}')

    record = queue[0]
    assert "TOK" not in record.text
    assert "TOK" not in record.data["output"]
    assert record.data["is_error"] is False


def test_every_string_field_is_bounded():
    big = "é" * consumer.MAX_FRAGMENT_FIELD_BYTES
    callback, queue = _held(_Mapper({"kind": "tool.started", "tool_name": "x",
                                     "text": big, "data": {"call_id": "c", "input": big}}))

    callback('{"type": "x"}')

    record = queue[0]
    assert len(record.text.encode()) <= consumer.MAX_FRAGMENT_FIELD_BYTES
    assert len(record.data["input"].encode()) <= consumer.MAX_FRAGMENT_FIELD_BYTES
    record.data["input"].encode().decode()      # cut on a codepoint


def test_a_partial_fragment_is_coalesced_not_queued_one_per_token(monkeypatch):
    """E9 B2: a token-level fragment goes to `submit_partial` (one row per flush), never to
    the per-record queue - that is what keeps 200 parallel runs to a paced write rate."""
    partials = []
    monkeypatch.setattr(execution_deltas, "submit_partial",
                        lambda execution_id, kind, text: partials.append((execution_id, kind, text)))
    callback, queue = _held(_Mapper([{"kind": "assistant.partial", "text": "He"},
                                     {"kind": "reasoning.partial", "text": "hm"}]))

    callback('{"type": "stream_event"}')

    assert partials == [("e1", "assistant.partial", "He"), ("e1", "reasoning.partial", "hm")]
    assert list(queue) == []


def test_the_consumer_closes_the_partial_buffer_of_every_run():
    """A run's held-back tail is flushed by `close`, from the consumer's per-job `finally`,
    so an error turn loses no streamed text. A call-graph check on the consumer."""
    tree = ast.parse((FRAMEWORK / "consumer.py").read_text())
    closes = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "close"
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "execution_deltas"]
    finals = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
              for f in n.finalbody for c in ast.walk(f) if c in closes]

    assert len(closes) == 1 and finals
