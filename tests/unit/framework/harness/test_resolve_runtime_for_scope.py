"""``resolve_runtime_for_scope`` gives a config tester the same (harness, provider,
model) a run at that scope would use — one resolution rule, not a second one."""
from __future__ import annotations

import pytest

from agento.framework.agent_view_runtime import resolve_runtime_for_scope
from agento.framework.scoped_config import Scope

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


@pytest.fixture
def db(monkeypatch):
    rows: dict[tuple[Scope, int], dict[str, tuple[str | None, bool]]] = {}

    def _load(_conn, scope, scope_id):
        return dict(rows.get((scope, scope_id), {}))

    monkeypatch.setattr("agento.framework.scoped_config.load_scoped_db_overrides", _load)
    monkeypatch.setattr(
        "agento.framework.config_resolver.ScopedConfigService._resolve_workspace_id",
        staticmethod(lambda _conn, _id: 3),
    )
    monkeypatch.setattr(
        "agento.framework.agent_view_runtime.get_agent_view",
        lambda _conn, av_id: _AgentView(av_id, 3) if av_id == 7 else None,
    )
    monkeypatch.setattr("agento.framework.agent_view_runtime.get_workspace", lambda *_: None)

    def put(scope: Scope, scope_id: int, path: str, value: str) -> None:
        rows.setdefault((scope, scope_id), {})[path] = (value, False)

    return put


class _AgentView:
    def __init__(self, id, workspace_id):
        self.id, self.workspace_id = id, workspace_id


def test_agent_view_scope_inherits_from_workspace_and_default(db):
    db(Scope.DEFAULT, 0, "agent_view/harness", "pi")
    db(Scope.WORKSPACE, 3, "agent_view/provider", "openrouter")
    db(Scope.AGENT_VIEW, 7, "agent_view/model", "deepseek/deepseek-v4-flash")

    assert resolve_runtime_for_scope(object(), Scope.AGENT_VIEW, 7) == (
        "pi", "openrouter", "deepseek/deepseek-v4-flash",
    )


def test_workspace_scope_ignores_agent_view_rows(db):
    db(Scope.WORKSPACE, 3, "agent_view/harness", "codex")
    db(Scope.WORKSPACE, 3, "agent_view/provider", "openai")
    db(Scope.WORKSPACE, 3, "agent_view/model", "gpt-5.4-mini")
    db(Scope.AGENT_VIEW, 7, "agent_view/model", "gpt-5.5")

    assert resolve_runtime_for_scope(object(), Scope.WORKSPACE, 3) == (
        "codex", "openai", "gpt-5.4-mini",
    )


def test_default_scope(db):
    db(Scope.DEFAULT, 0, "agent_view/harness", "codex")
    db(Scope.DEFAULT, 0, "agent_view/provider", "openai")
    db(Scope.WORKSPACE, 3, "agent_view/harness", "pi")

    harness, provider, _model = resolve_runtime_for_scope(object(), Scope.DEFAULT, 0)

    assert (harness, provider) == ("codex", "openai")


def test_pre_0_15_provider_holding_a_harness_id(db):
    db(Scope.WORKSPACE, 3, "agent_view/provider", "codex")

    harness, provider, _ = resolve_runtime_for_scope(object(), Scope.WORKSPACE, 3)

    assert (harness, provider) == ("codex", "openai")


def test_invalid_provider_raises_like_a_run(db):
    db(Scope.AGENT_VIEW, 7, "agent_view/harness", "claude")
    db(Scope.AGENT_VIEW, 7, "agent_view/provider", "openrouter")

    with pytest.raises(ValueError):
        resolve_runtime_for_scope(object(), Scope.AGENT_VIEW, 7)
