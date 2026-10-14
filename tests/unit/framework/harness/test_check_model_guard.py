"""CLS-1 guard: every in-tree harness can check its models.

`check_model` is optional for an out-of-tree harness (harness/protocols.py). An in-tree
harness without it would make the `agent_view/model` tester answer MODEL_NOT_CHECKED
for every model.
"""
from __future__ import annotations

import inspect

import pytest

from agento.framework.harness import list_harnesses

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


def test_every_in_tree_adapter_has_check_model():
    harnesses = list_harnesses()
    assert {h.descriptor.id for h in harnesses} >= {"claude", "codex", "pi"}
    for h in harnesses:
        check = getattr(h.adapter, "check_model", None)
        assert callable(check), h.descriptor.id
        params = inspect.signature(check).parameters
        assert list(params)[:3] == ["provider", "model", "credential"], h.descriptor.id
        assert params["timeout_s"].kind is inspect.Parameter.KEYWORD_ONLY, h.descriptor.id
