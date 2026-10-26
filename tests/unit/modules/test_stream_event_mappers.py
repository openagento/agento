"""Every shipped harness adapter exposes its StreamEventMapper (E9 §3.3)."""
from __future__ import annotations

import pytest

from agento.framework.harness.protocols import StreamEventMapper
from agento.modules.claude.src.adapter import ClaudeHarnessAdapter
from agento.modules.claude.src.stream_event_mapper import ClaudeStreamEventMapper
from agento.modules.codex.src.adapter import CodexHarnessAdapter
from agento.modules.codex.src.stream_event_mapper import CodexStreamEventMapper
from agento.modules.pi.src.adapter import PiHarnessAdapter
from agento.modules.pi.src.stream_event_mapper import PiStreamEventMapper


@pytest.mark.parametrize(("adapter", "mapper_class"), [
    pytest.param(ClaudeHarnessAdapter, ClaudeStreamEventMapper, id="claude"),
    pytest.param(CodexHarnessAdapter, CodexStreamEventMapper, id="codex"),
    pytest.param(PiHarnessAdapter, PiStreamEventMapper, id="pi"),
])
def test_the_adapter_exposes_the_mapper(adapter, mapper_class):
    mapper = adapter().stream_event_mapper
    assert isinstance(mapper, mapper_class)
    assert isinstance(mapper, StreamEventMapper)
