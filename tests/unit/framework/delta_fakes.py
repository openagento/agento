"""Fake delta sinks (framework/execution_deltas.py): the framework owns no rows, so a sink
only records the batches it was given."""
from __future__ import annotations

import threading

from agento.framework.execution_hooks import DeltaRecord


class FakeSink:
    def __init__(self) -> None:
        self.batches: list[list[DeltaRecord]] = []
        self.lock = threading.Lock()

    def write(self, batch) -> None:
        with self.lock:
            self.batches.append(list(batch))

    def records(self) -> list[DeltaRecord]:
        with self.lock:
            return [r for b in self.batches for r in b]


class HeldSink(FakeSink):
    """Each write waits, inside the handover lock, until ``gate`` is set."""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self.gate = gate
        self.inside = threading.Event()

    def write(self, batch) -> None:
        self.inside.set()
        assert self.gate.wait(10), "the test never opened the gate"
        super().write(batch)
