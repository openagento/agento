"""Live text at scale: 250 parallel runs through the real writer and sink (E9 chat UX, plan B6).

The owner's bar (2026-10-06): token streaming must not trade off scalability at 100-200
parallel jobs (RULES.md SCL-1). This drives the real `_delta_callback` (with the claude
mapper), the real paced `execution_deltas` writer and the real `ConversationDeltaSink` on
MySQL, and asserts the bounds plan B5 states: no loss, stream order per execution, paced
transactions, bounded partial rows and bounded commit lag.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid

import pytest

from agento.framework import consumer, execution_deltas
from agento.modules.claude.src.stream_event_mapper import ClaudeStreamEventMapper
from agento.modules.conversation.src import service
from agento.modules.conversation.src.deltas import ConversationDeltaSink

from .conftest import _clean  # noqa: F401

PANEL_RUNS = 200
SHARED_RUNS = 50                 # on ONE channel thread
FAST_RUNS = 20                   # 64 KiB of text in < 1 s
SWITCH_RUNS = 20                 # reasoning/text alternate every 5 deltas
REPLAY_S = 3.0
SECRET = "cap_scale_secret_0123456789"


class _Entry:
    class adapter:  # noqa: N801 - the shape `_delta_callback` reads
        stream_event_mapper = ClaudeStreamEventMapper()


class CountingSink:
    """The real sink, timed: one `write` is one transaction."""

    def __init__(self) -> None:
        self.inner = ConversationDeltaSink()
        self.writes: list[tuple[float, int]] = []
        self.committed: dict[tuple[str, str], float] = {}
        self.partials: list[tuple[str, str, float]] = []
        self.errors: list[BaseException] = []
        self.lock = threading.Lock()

    def write(self, batch) -> None:
        try:
            self.inner.write(batch)
        except BaseException as exc:      # the writer would swallow it; the test must not
            self.errors.append(exc)
            raise
        now = time.monotonic()
        with self.lock:
            self.writes.append((now, len(batch)))
            for r in batch:
                if r.kind.endswith(".partial"):
                    self.partials.append((r.execution_id, r.text or "", now))
                    continue
                key = (r.data or {}).get("call_id") or r.text or ""
                self.committed.setdefault((r.execution_id, f"{r.kind}:{key}"), now)


def _delta(kind: str, text: str) -> dict:
    field = "text" if kind == "text_delta" else "thinking"
    return {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                              "delta": {"type": kind, field: text}}}


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _script(n: int, style: str) -> list[tuple[float, dict]]:
    """(delay before, event) pairs: a claude partial-messages turn with two tool calls."""
    words = [f"w{n}_{i} " for i in range(60)]
    out: list[tuple[float, dict]] = []
    if style == "fast":
        chunk = "x" * 512
        out += [(0.0, _delta("text_delta", chunk)) for _ in range(128)]          # 64 KiB
        out.append((0.0, _assistant({"type": "text", "text": chunk * 128})))
    elif style == "switch":
        for i in range(60):
            kind = "thinking_delta" if (i // 5) % 2 == 0 else "text_delta"
            out.append((REPLAY_S / 120, _delta(kind, words[i])))
    else:
        out += [(REPLAY_S / 120, _delta("thinking_delta", w)) for w in words[:20]]
        out.append((0.0, _assistant({"type": "thinking", "thinking": "".join(words[:20])})))
        out += [(REPLAY_S / 120, _delta("text_delta", w)) for w in words[20:40]]
        # A capability token split across two deltas must never reach a row (SEC-6).
        out += [(0.01, _delta("text_delta", SECRET[:9])), (0.01, _delta("text_delta", SECRET[9:]))]
    for t in (1, 2):
        out.append((0.05, _assistant({"type": "tool_use", "id": f"r{n}-t{t}", "name": "Bash",
                                      "input": {"command": f"echo {t}"}})))
        out.append((REPLAY_S / 12, {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"r{n}-t{t}", "content": "ok"}]}}))
    out += [(REPLAY_S / 120, _delta("text_delta", w)) for w in words[40:]]
    out.append((0.0, _assistant({"type": "text", "text": f"final answer {n}"})))
    return out


@pytest.fixture
def scale(conn, world):
    ids: list[tuple[str, int]] = []
    with conn.cursor() as cur:
        conversations = []
        for i in range(PANEL_RUNS):
            cur.execute("INSERT INTO conversation (user_id, agent_view_id, title, status) "
                        "VALUES (%s, %s, %s, 'active')", (world["owner"].id, world["view"], f"s{i}"))
            conversations.append(cur.lastrowid)
        cur.execute("INSERT INTO conversation (user_id, agent_view_id, title, status, channel) "
                    "VALUES (NULL, %s, 'shared', 'active', 'jira')", (world["view"],))
        conversations += [cur.lastrowid] * SHARED_RUNS
        for i, conversation_id in enumerate(conversations):
            execution_id = str(uuid.uuid4())
            cur.execute("INSERT INTO execution (execution_id, job_id, attempt, status, "
                        "conversation_id) VALUES (%s, %s, 1, 'running', %s)",
                        (execution_id, 900000 + i, conversation_id))
            ids.append((execution_id, conversation_id))
    conn.commit()
    yield ids
    with conn.cursor() as cur:
        cur.execute("DELETE FROM execution_delta")
        cur.execute("DELETE FROM conversation_event")
        cur.execute("DELETE FROM execution")
        cur.execute("DELETE FROM conversation")
    conn.commit()


def test_250_parallel_streaming_runs_stay_ordered_paced_and_lossless(conn, scale):
    sink = CountingSink()
    execution_deltas.sync(sink, module="conversation", class_path="src.deltas.Scale")
    fed: dict[tuple[str, str], float] = {}
    fed_lock = threading.Lock()
    fed_words: dict[tuple[str, str], float] = {}
    styles = (["fast"] * FAST_RUNS + ["switch"] * SWITCH_RUNS
              + ["normal"] * (len(scale) - FAST_RUNS - SWITCH_RUNS))

    def run(n: int, execution_id: str, style: str) -> None:
        on_line = consumer._delta_callback(_Entry(), execution_id, logging.getLogger("scale"),
                                           (SECRET,))
        for delay, event in _script(n, style):
            time.sleep(delay)
            on_line(json.dumps(event))
            for fragment in _as_list(ClaudeStreamEventMapper().map_event(event)):
                if fragment["kind"].endswith(".partial") and fragment["text"].startswith("w"):
                    with fed_lock:      # each word is unique per run: a row's first word dates it
                        fed_words[(execution_id, fragment["text"].split()[0])] = time.monotonic()
                if fragment["kind"] in ("tool.started", "tool.completed", "assistant.text"):
                    key = (fragment.get("data") or {}).get("call_id") or fragment.get("text")
                    with fed_lock:
                        fed[(execution_id, f"{fragment['kind']}:{key}")] = time.monotonic()
        execution_deltas.close(execution_id)

    started = time.monotonic()
    threads = [threading.Thread(target=run, args=(n, e, styles[n]))
               for n, (e, _) in enumerate(scale)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    deadline = time.monotonic() + 30
    while execution_deltas._worker.pending() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(2 * execution_deltas.WRITE_INTERVAL_S)    # the last paced write
    elapsed = time.monotonic() - started
    execution_deltas._reset_for_tests()

    assert not sink.errors, sink.errors[:3]               # no 1205/1213 escaped the retry
    with conn.cursor() as cur:
        cur.execute("SELECT id, execution_id, kind, payload FROM conversation_event "
                    "WHERE source_kind = 'delta' ORDER BY id")
        rows = list(cur.fetchall())
    conn.commit()

    assert not [r for r in rows if r["kind"] in ("gap", "truncated")]
    assert not [r for r in rows if SECRET[:9] in (r["payload"] or "") or SECRET in r["payload"]]
    by_run: dict[str, list[dict]] = {}
    for r in rows:
        by_run.setdefault(r["execution_id"], []).append(r)
    max_partials = service_cap_half()
    for n, (execution_id, _) in enumerate(scale):
        mine = by_run.get(execution_id, [])
        kinds = [r["kind"] for r in mine]
        assert f"final answer {n}" in [json.loads(r["payload"])["text"] for r in mine
                                       if r["kind"] == "assistant.text"], (n, styles[n])
        assert kinds.count("tool.completed") == 2
        seqs = [json.loads(r["payload"])["seq"] for r in mine]
        assert seqs == sorted(seqs), (n, styles[n])        # ids follow seq: one order
        partials = [r for r in mine if r["kind"].endswith(".partial")]
        streamed = sum(len(json.loads(r["payload"])["text"].encode()) for r in partials)
        segments = 2 if styles[n] == "normal" else 12 if styles[n] == "switch" else 1
        bound = elapsed / 0.25 + segments + streamed / execution_deltas.PARTIAL_FLUSH_BYTES + 2
        assert len(partials) <= min(bound, max_partials), (n, styles[n], len(partials))

    total_rows = sum(size for _, size in sink.writes)
    assert len(sink.writes) <= elapsed / execution_deltas.WRITE_INTERVAL_S \
        + total_rows / execution_deltas.BATCH + 2, (len(sink.writes), total_rows, elapsed)
    lags = sorted(sink.committed[k] - t for k, t in fed.items() if k in sink.committed)
    assert len(lags) == len(fed)
    assert lags[int(len(lags) * 0.95)] <= 1.0, lags[-10:]
    assert lags[-1] <= 2.0, lags[-10:]
    # Live text too (review impl-1 F3): a partial row is committed within the same bound,
    # counted from its OLDEST piece - the latency a reader sees before the text appears.
    live = sorted(t - fed_words[(e, text.split()[0])] for e, text, t in sink.partials
                  if text.split() and (e, text.split()[0]) in fed_words)
    assert len(live) > PANEL_RUNS, len(live)
    assert live[int(len(live) * 0.95)] <= 1.0, live[-10:]
    assert live[-1] <= 2.0, live[-10:]
    assert elapsed <= REPLAY_S + 20


def service_cap_half() -> int:
    from agento.framework.database_config import DatabaseConfig
    from agento.framework.db import get_connection
    conn = get_connection(DatabaseConfig.from_env())
    try:
        return service.config(conn, "stream/max_deltas_per_execution") // 2
    finally:
        conn.close()


def _as_list(mapped) -> list[dict]:
    return mapped if isinstance(mapped, list) else [mapped] if mapped else []
