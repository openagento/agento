from __future__ import annotations

import asyncio
import threading

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, SelectionList, Static

from agento.framework.scoped_config import Scope

from ..data import EnablementItem, set_config_value
from ..widgets.scope_selector import ScopeChanged, ScopeSelector
from ..widgets.sidebar import Sidebar


def prompt_label(item: EnablementItem) -> str:
    """Checkbox label. Mark a tool its toolset master blocks, so the operator can
    tell "off here" from "on here but denied upstream"; otherwise mark enables
    resolved from a parent scope as inherited, so a local grant is distinguishable
    from one inherited up the scope chain."""
    if item.blocked_by:
        return f"{item.name}  (blocked by {item.blocked_by})"
    if item.enabled and not item.explicit_here:
        return f"{item.name}  (inherited)"
    return item.name


class EnablementScreen(Screen):
    """Shared base for the opt-in Skills and Tools screens.

    Owns the scope selector, the serialized DB workers (``app.conn`` is a single
    non-thread-safe pymysql connection — two workers must not touch it at once),
    per-item toggle persistence, and the refresh action. Subclasses set
    ``SIDEBAR_KEY``/``HINT`` and implement ``fetch``/``empty``/``render_data``.
    """

    SIDEBAR_KEY: str = ""
    HINT: str = ""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._scope: str = Scope.DEFAULT
        self._scope_id: int = 0
        self._db_lock = threading.Lock()
        # Serializes renders so overlapping loads (e.g. rapid scope switches)
        # can't interleave remove_children()/mount() and leave duplicate widgets.
        self._render_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Sidebar(active=self.SIDEBAR_KEY)
        with Vertical(classes="screen-content"):
            yield ScopeSelector(classes="enablement-top")
            yield Static(self.HINT, classes="enablement-hint")
            yield VerticalScroll(classes="enablement-lists")
        yield Footer()

    def on_mount(self) -> None:
        self._load_scope_options()
        self._load_states()

    @work(thread=True)
    def _load_scope_options(self) -> None:
        conn = self.app.conn
        if conn is None:
            return
        selector = self.query_one(ScopeSelector)
        with self._db_lock:
            selector.load_options(conn)

    def on_scope_changed(self, message: ScopeChanged) -> None:
        # Ignore no-op events — loading the scope dropdowns emits a ScopeChanged
        # for the unchanged (default) scope on mount, which would otherwise
        # trigger a redundant second render.
        if message.scope == self._scope and message.scope_id == self._scope_id:
            return
        self._scope = message.scope
        self._scope_id = message.scope_id
        self._load_states()

    @work(thread=True)
    def _load_states(self) -> None:
        conn = self.app.conn
        with self._db_lock:
            data = self.fetch(conn) if conn is not None else self.empty()
        self.app.call_from_thread(self._render_locked, data)

    async def _render_locked(self, data) -> None:
        async with self._render_lock:
            await self.render_data(data)

    def action_refresh(self) -> None:
        self._load_states()

    def on_selection_list_selection_toggled(self, event: SelectionList.SelectionToggled) -> None:
        path = str(event.selection.value)
        value = "1" if event.selection.value in event.selection_list.selected else "0"
        self.write_paths([(path, value)])

    def write_paths(self, items: list[tuple[str, str]]) -> None:
        """Persist (path, value) writes at the current scope (in a worker)."""
        self._write_paths(items, self._scope, self._scope_id)

    @work(thread=True)
    def _write_paths(self, items: list[tuple[str, str]], scope: str, scope_id: int) -> None:
        conn = self.app.conn
        if conn is None:
            self.app.call_from_thread(self.notify, "No database connection", severity="error")
            return
        try:
            with self._db_lock:
                for path, value in items:
                    set_config_value(conn, path, value, scope, scope_id)
            scope_label = scope + (f" (id={scope_id})" if scope_id else "")
            if len(items) == 1:
                verb = "Enabled" if items[0][1] == "1" else "Disabled"
                target = items[0][0]
            else:
                verb = "Enabled" if items and items[0][1] == "1" else "Disabled"
                target = f"{len(items)} tools"
            self.app.call_from_thread(self.notify, f"{verb} {target} [{scope_label}]")
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Save failed: {e}", severity="error")

    # ---- subclass hooks ----
    def fetch(self, conn):
        raise NotImplementedError

    def empty(self):
        raise NotImplementedError

    async def render_data(self, data, /) -> None:
        raise NotImplementedError
