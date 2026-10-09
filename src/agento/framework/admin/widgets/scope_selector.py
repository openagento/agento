from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Select

from ...scoped_config import Scope


class ScopeChanged(Message):
    def __init__(self, scope: str, scope_id: int) -> None:
        self.scope = scope
        self.scope_id = scope_id
        super().__init__()


class ModeChanged(Message):
    def __init__(self, mode: str) -> None:
        self.mode = mode  # "all" or "overrides"
        super().__init__()


class ScopeSelector(Widget):

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._workspaces: list[dict] = []
        self._agent_views: list[dict] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="scope-bar"):
            yield Select(
                [(Scope.DEFAULT, Scope.DEFAULT), (Scope.WORKSPACE, Scope.WORKSPACE), (Scope.AGENT_VIEW, Scope.AGENT_VIEW)],
                prompt="Scope",
                value=Scope.DEFAULT,
                allow_blank=False,
                id="scope-type",
            )
            yield Select([], prompt="Workspace", id="scope-workspace", disabled=True)
            yield Select([], prompt="Agent View", id="scope-agent-view", disabled=True)
            yield Select(
                [("Browse All", "all"), ("Overrides Only", "overrides")],
                prompt="Mode",
                value="all",
                allow_blank=False,
                id="scope-mode",
            )

    def load_options(self, conn) -> None:
        from ..data import get_agent_views, get_workspaces

        self._workspaces = get_workspaces(conn)
        self._agent_views = get_agent_views(conn)

        ws_select = self.query_one("#scope-workspace", Select)
        ws_options = [(w["code"], w["id"]) for w in self._workspaces]
        ws_select.set_options(ws_options)

    @property
    def scope(self) -> str:
        val = self.query_one("#scope-type", Select).value
        if val is Select.BLANK:
            return Scope.DEFAULT
        return str(val)

    @property
    def scope_id(self) -> int:
        scope = self.scope
        if scope == Scope.AGENT_VIEW:
            val = self.query_one("#scope-agent-view", Select).value
            return int(val) if val is not Select.BLANK else 0
        if scope == Scope.WORKSPACE:
            val = self.query_one("#scope-workspace", Select).value
            return int(val) if val is not Select.BLANK else 0
        return 0

    @property
    def mode(self) -> str:
        val = self.query_one("#scope-mode", Select).value
        if val is Select.BLANK:
            return "all"
        return str(val)

    def on_select_changed(self, event: Select.Changed) -> None:
        select_id = event.select.id

        if select_id == "scope-type":
            self._on_scope_type_changed()
        elif select_id == "scope-workspace":
            self._on_workspace_changed()
        elif select_id == "scope-agent-view":
            self.post_message(ScopeChanged(self.scope, self.scope_id))
        elif select_id == "scope-mode":
            self.post_message(ModeChanged(self.mode))

    def _on_scope_type_changed(self) -> None:
        scope = self.scope
        ws_select = self.query_one("#scope-workspace", Select)
        av_select = self.query_one("#scope-agent-view", Select)

        if scope == Scope.DEFAULT:
            ws_select.disabled = True
            av_select.disabled = True
        elif scope == Scope.WORKSPACE:
            ws_select.disabled = False
            av_select.disabled = True
        elif scope == Scope.AGENT_VIEW:
            ws_select.disabled = False
            av_select.disabled = False
            self._refresh_agent_views()

        self.post_message(ScopeChanged(self.scope, self.scope_id))

    def _on_workspace_changed(self) -> None:
        if self.scope == Scope.AGENT_VIEW:
            self._refresh_agent_views()
        self.post_message(ScopeChanged(self.scope, self.scope_id))

    def _refresh_agent_views(self) -> None:
        ws_val = self.query_one("#scope-workspace", Select).value
        ws_id = int(ws_val) if ws_val is not Select.BLANK else None
        av_select = self.query_one("#scope-agent-view", Select)

        if ws_id is not None:
            filtered = [av for av in self._agent_views if av["workspace_id"] == ws_id]
        else:
            filtered = self._agent_views

        av_options = [(av["code"], av["id"]) for av in filtered]
        av_select.set_options(av_options)
