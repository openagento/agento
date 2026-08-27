from __future__ import annotations

import json

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Select, Static, TextArea, Tree

from agento.framework.scoped_config import Scope

from ..data import ResolvedField
from ..widgets.confirm import ConfirmScreen
from ..widgets.field_detail import FieldDetailPanel
from ..widgets.scope_selector import ModeChanged, ScopeChanged, ScopeSelector
from ..widgets.sidebar import Sidebar

_RESULT_LABEL = {
    "ok": ("OK", "information"),
    "fail": ("FAIL", "error"),
    "error": ("ERROR", "error"),
    "not_configured": ("NOT_CONFIGURED", "warning"),
}


def _format_result(result) -> tuple[str, str]:
    """(line, notify severity) for one TestResult."""
    label, severity = _RESULT_LABEL.get(result.status, (result.status.upper(), "warning"))
    code = f" [{result.code}]" if result.code and result.code != label else ""
    return f"{label}{code} — {result.message}", severity


class ConfigScreen(Screen):

    BINDINGS = [  # noqa: RUF012
        Binding("e", "edit_field", "e Edit", show=True, priority=True),
        Binding("d", "delete_entry", "d Delete", show=True, priority=True),
        Binding("t", "test_field", "t Test", show=True, priority=True),
        Binding("m", "toggle_mode", "m Mode", show=True),
        Binding("slash", "focus_search", "/ Search", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_module: str | None = None
        self._current_scope: str = Scope.DEFAULT
        self._current_scope_id: int = 0
        self._current_mode: str = "all"
        self._fields: list[ResolvedField] = []
        self._search_text: str = ""
        self._just_highlighted = False

    def compose(self) -> ComposeResult:
        yield Sidebar(active="config")
        with Vertical(classes="screen-content"):
            yield ScopeSelector(id="config-top")
            with Horizontal(id="config-main"):
                yield Tree("Modules", id="module-tree")
                with Vertical(id="field-table-container"):
                    yield Input(placeholder="Filter fields...", id="field-search")
                    yield DataTable(id="field-table")
            yield FieldDetailPanel(id="field-detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#field-table", DataTable)
        table.add_columns("Field", "Value", "Source")
        table.cursor_type = "row"
        self._load_tree()
        self._load_scope_options()

    @work(thread=True)
    def _load_tree(self) -> None:
        from ..data import get_module_schemas

        schemas = get_module_schemas()
        self.app.call_from_thread(self._populate_tree, schemas)

    def _populate_tree(self, schemas) -> None:
        tree = self.query_one("#module-tree", Tree)
        tree.clear()
        for schema in schemas:
            node = tree.root.add(schema.name, data={"type": "module", "name": schema.name})
            if schema.tools:
                tools_node = node.add("tools", data={"type": "tools_folder", "module": schema.name})
                for tool_name in schema.tools:
                    tools_node.add_leaf(
                        tool_name, data={"type": "tool", "module": schema.name, "tool": tool_name}
                    )
        tree.root.expand_all()

    @work(thread=True)
    def _load_scope_options(self) -> None:
        conn = self.app.conn
        if conn is None:
            return
        selector = self.query_one(ScopeSelector)
        selector.load_options(conn)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if data is None:
            return
        if data["type"] == "module":
            self._current_module = data["name"]
            self._load_fields()
        elif data["type"] == "tool":
            self._current_module = data["module"]
            self._load_fields(tool_filter=data["tool"])

    def on_scope_changed(self, message: ScopeChanged) -> None:
        self._current_scope = message.scope
        self._current_scope_id = message.scope_id
        if self._current_module:
            self._load_fields()

    def on_mode_changed(self, message: ModeChanged) -> None:
        self._current_mode = message.mode
        self._refresh_table()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "field-search":
            self._search_text = event.value.strip().lower()
            self._refresh_table()

    @work(thread=True)
    def _load_fields(self, tool_filter: str | None = None) -> None:
        conn = self.app.conn
        if conn is None or self._current_module is None:
            return

        from ..data import get_resolved_fields

        fields = get_resolved_fields(conn, self._current_module, self._current_scope, self._current_scope_id)

        if tool_filter:
            prefix = f"{self._current_module}/tools/{tool_filter}/"
            fields = [f for f in fields if f.path.startswith(prefix)]

        self.app.call_from_thread(self._update_fields, fields)

    def _update_fields(self, fields: list[ResolvedField]) -> None:
        self._fields = fields
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#field-table", DataTable)
        table.clear()

        filtered = self._fields
        if self._current_mode == "overrides":
            filtered = [f for f in filtered if f.source == "db"]
        if self._search_text:
            filtered = [f for f in filtered if self._search_text in f.field_name.lower()]

        for field in filtered:
            table.add_row(field.field_name, field.display_value, field.source, key=field.path)

        # Clear detail panel
        self.query_one(FieldDetailPanel).update_field(None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._just_highlighted:
            self._just_highlighted = False
            return
        self.action_edit_field()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._just_highlighted = True
        if event.row_key is None:
            return
        path = str(event.row_key.value)
        field = self._find_field(path)
        panel = self.query_one(FieldDetailPanel)
        # The panel shows the "Last:" line for THIS scope only, so it has to be
        # told which scope it is rendering before it renders.
        panel.set_scope(self._current_scope, self._current_scope_id)
        panel.update_field(field)

    def _find_field(self, path: str) -> ResolvedField | None:
        for f in self._fields:
            if f.path == path:
                return f
        return None

    def _get_selected_field(self) -> ResolvedField | None:
        table = self.query_one("#field-table", DataTable)
        if table.cursor_row < 0 or table.row_count == 0:
            return None
        try:
            from textual.coordinate import Coordinate

            cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
            path = str(cell_key.row_key.value)
            return self._find_field(path)
        except Exception:
            return None

    def action_edit_field(self) -> None:
        field = self._get_selected_field()
        if field is None:
            self.notify("No field selected", severity="warning")
            return
        if field.source == "env":
            self.notify("Cannot edit environment variable overrides", severity="warning")
            return
        if not field.editable_at_scope:
            allowed = ", ".join(field.allowed_scopes) or "none"
            self.notify(
                f"Field is read-only at scope '{self._current_scope}' "
                f"(configurable at: {allowed})",
                severity="warning",
            )
            return
        self.app.push_screen(
            ConfigFieldEditorScreen(field, self._current_scope, self._current_scope_id),
            callback=self._on_editor_dismiss,
        )

    def _on_editor_dismiss(self, saved: bool | None) -> None:
        if saved:
            self._load_fields()

    def action_test_field(self) -> None:
        field = self._get_selected_field()
        if field is None:
            self.notify("No field selected", severity="warning")
            return
        if not getattr(field, "tester", ""):
            self.notify("This field has no test action", severity="warning")
            return
        self.notify(f"Testing {field.path}…")
        self._do_test(field, self._current_scope, self._current_scope_id)

    @work(thread=True)
    def _do_test(self, field, scope: str, scope_id: int) -> None:
        """Run the tester off the UI thread — a network probe takes seconds.

        Nothing here touches a Textual object directly, the DOM query included:
        `query_one` is a method call on a Textual object, and Textual documents
        those as having unpredictable results off the UI thread. The scope is
        passed in rather than read from `self` for the same reason. Everything
        that touches the UI lives in `_show_test_result`, which
        `call_from_thread` runs back on the UI thread.
        """
        from agento.framework.config_test import run_config_test

        conn = self.app.conn
        if conn is None:
            self.app.call_from_thread(
                self.notify, "No database connection", severity="error"
            )
            return
        result = run_config_test(conn, field.path, scope=scope, scope_id=scope_id)
        line, severity = _format_result(result)
        self.app.call_from_thread(
            self._show_test_result, field, line, severity, scope, scope_id,
        )

    def _show_test_result(self, field, line: str, severity: str, scope, scope_id) -> None:
        """UI-thread half of `_do_test`: one query, the updates, no network.

        A probe takes seconds, and the operator can move on while it runs. The
        result is filed under the scope it was measured at, and the panel is only
        re-rendered when the selection AND the scope still match — otherwise a
        default-scope verdict would land on whatever field is now selected, at
        whatever scope is now chosen.
        """
        panel = self.query_one(FieldDetailPanel)
        panel.set_test_result(field.path, line, scope, scope_id)
        self.notify(f"{field.path}: {line}", severity=severity)
        current = self._get_selected_field()
        if (self._current_scope, self._current_scope_id) != (scope, scope_id):
            return
        if current is None or current.path != field.path:
            return
        panel.update_field(current)

    def action_delete_entry(self) -> None:
        field = self._get_selected_field()
        if field is None:
            self.notify("No field selected", severity="warning")
            return
        if not field.editable_at_scope:
            allowed = ", ".join(field.allowed_scopes) or "none"
            self.notify(
                f"Field is read-only at scope '{self._current_scope}' "
                f"(configurable at: {allowed})",
                severity="warning",
            )
            return
        if field.source != "db":
            self.notify("Only DB overrides can be deleted", severity="warning")
            return

        def _on_confirm(confirmed: bool) -> None:
            if confirmed:
                self._do_delete(field)

        self.app.push_screen(
            ConfirmScreen(f"Do you want to remove this entry '{field.path}'?", default_no=True),
            callback=_on_confirm,
        )

    @work(thread=True)
    def _do_delete(self, field: ResolvedField) -> None:
        from ..data import delete_config_override

        conn = self.app.conn
        if conn is None:
            return
        deleted = delete_config_override(conn, field.path, self._current_scope, self._current_scope_id)
        if deleted:
            self.app.call_from_thread(self.notify, f"Deleted override: {field.path}")
        else:
            self.app.call_from_thread(self.notify, "Override not found", severity="warning")

        # Reload fields
        from ..data import get_resolved_fields

        fields = get_resolved_fields(conn, self._current_module, self._current_scope, self._current_scope_id)
        self.app.call_from_thread(self._update_fields, fields)

    def action_toggle_mode(self) -> None:
        selector = self.query_one(ScopeSelector)
        mode_select = selector.query_one("#scope-mode", Select)
        new_mode = "overrides" if self._current_mode == "all" else "all"
        mode_select.value = new_mode

    def action_focus_search(self) -> None:
        self.query_one("#field-search", Input).focus()

    def action_refresh(self) -> None:
        from ..data import clear_module_schema_cache

        clear_module_schema_cache()
        self._load_tree()
        if self._current_module:
            self._load_fields()


class ConfigFieldEditorScreen(ModalScreen[bool]):

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "cancel", "Esc Cancel", show=True),
    ]

    def __init__(self, field: ResolvedField, scope: str, scope_id: int) -> None:
        super().__init__()
        self._field = field
        self._scope = scope
        self._scope_id = scope_id

    def compose(self) -> ComposeResult:
        field = self._field
        current = field.value if field.value is not None else ""

        with Vertical(id="editor-dialog"):
            yield Static(f"Edit: {field.path}", classes="panel-title")
            yield Static(f"Label:  {field.label}")
            yield Static(f"Type:   {field.field_type}")
            yield Static(f"Scope:  {self._scope}" + (f" (id={self._scope_id})" if self._scope_id else ""))
            yield Static(f"Current: {field.display_value} [{field.source}]")
            if field.description:
                yield Static(f"Info:   {field.description}")
            yield Static("")

            if field.field_type == "boolean":
                yield Select(
                    [("true", "true"), ("false", "false")],
                    value=current if current in ("true", "false") else "true",
                    allow_blank=False,
                    id="editor-input",
                )
            elif field.field_type == "select" and field.options:
                select_options = [(opt["label"], opt["value"]) for opt in field.options]
                allowed_values = [opt["value"] for opt in field.options]
                yield Select(
                    select_options,
                    value=current if current in allowed_values else Select.BLANK,
                    allow_blank=False,
                    id="editor-input",
                )
            elif field.field_type in ("json", "textarea"):
                yield TextArea(current, id="editor-textarea")
            elif field.field_type == "obscure":
                yield Input(value=current, password=True, id="editor-input")
            else:
                yield Input(value=current, id="editor-input")

            with Horizontal(id="editor-buttons"):
                yield Button("Save", variant="primary", id="editor-save")
                yield Button("Cancel", id="editor-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "editor-save":
            self._save()
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _get_value(self) -> str:
        if self._field.field_type in ("json", "textarea"):
            return self.query_one("#editor-textarea", TextArea).text
        if self._field.field_type == "boolean":
            val = self.query_one("#editor-input", Select).value
            return str(val) if val is not Select.BLANK else "true"
        if self._field.field_type == "select":
            val = self.query_one("#editor-input", Select).value
            return str(val) if val is not Select.BLANK else ""
        return self.query_one("#editor-input", Input).value

    def _validate(self, value: str) -> str | None:
        # The incident came through `config:set`, but this screen writes the same
        # field and had no validation at all. Same parse, same rule as
        # _is_private_key_field in framework/cli/config.py: an obscure field whose
        # name ends in ssh_private_key.
        if self._field.field_type == "obscure" and self._field.field_name.rsplit(
            "/", 1
        )[-1] == "ssh_private_key" and value.strip():
            from agento.framework.ssh_keys import EncryptedKeyError, derive_public_key

            try:
                derive_public_key(value)
            except EncryptedKeyError:
                return "Passphrase-protected keys cannot be used by the agent"
            except ValueError:
                return "Not a valid SSH private key (truncated paste?)"
        if self._field.field_type == "integer":
            try:
                int(value)
            except ValueError:
                return "Value must be a valid integer"
        elif self._field.field_type == "json":
            try:
                json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return "Value must be valid JSON"
        elif self._field.field_type == "boolean" and value not in ("true", "false"):
            return "Value must be 'true' or 'false'"
        elif self._field.field_type == "select" and self._field.options:
            allowed = [opt["value"] for opt in self._field.options]
            if value not in allowed:
                return f"Value must be one of: {', '.join(allowed)}"
        return None

    def _save(self) -> None:
        value = self._get_value()
        error = self._validate(value)
        if error:
            self.notify(error, severity="error")
            return
        self._do_save(value)

    @work(thread=True)
    def _do_save(self, value: str) -> None:
        from ..data import set_config_value

        conn = self.app.conn
        if conn is None:
            self.app.call_from_thread(self.notify, "No database connection", severity="error")
            return

        try:
            set_config_value(conn, self._field.path, value, self._scope, self._scope_id)
            self.app.call_from_thread(self.notify, f"Saved: {self._field.path}")
            self.app.call_from_thread(self.dismiss, True)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Save failed: {e}", severity="error")
