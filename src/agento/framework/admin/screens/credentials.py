from __future__ import annotations

from datetime import UTC, datetime

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Static

from ..widgets.confirm import ConfirmScreen
from ..widgets.sidebar import Sidebar

_CREDENTIAL_SEARCH_KEYS = ("id", "scope", "label", "status")


def _fmt_when(when) -> str:
    """Format a datetime/None as a short relative string for the UI."""
    if when is None:
        return "never"
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return when
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    secs = int((now - when).total_seconds())
    if secs < 0:
        abs_s = -secs
        if abs_s < 60:
            return f"in {abs_s}s"
        if abs_s < 3600:
            return f"in {abs_s // 60}m"
        if abs_s < 86400:
            return f"in {abs_s // 3600}h"
        return f"in {abs_s // 86400}d"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


class CredentialUsageScreen(ModalScreen):

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "dismiss", "Esc Close", show=True),
    ]

    DEFAULT_CSS = """
    CredentialUsageScreen {
        align: center middle;
    }
    #token-detail {
        width: 70;
        height: auto;
        max-height: 20;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #token-detail-title {
        text-style: bold;
        margin-bottom: 1;
    }
    """

    def __init__(self, token: dict) -> None:
        self.token = token
        super().__init__()

    def compose(self) -> ComposeResult:
        t = self.token
        with VerticalScroll(id="token-detail"):
            yield Static(
                f"Credential #{t['id']} -- {t['label']}",
                id="token-detail-title",
            )
            status = t.get("status", "ok")
            enabled = "yes" if t["enabled"] else "no"
            limit = str(t["token_limit"]) if t["token_limit"] > 0 else "unlimited"
            pct = f"{t['pct_free']:.1f}%" if t["token_limit"] > 0 else "-"
            used_at = _fmt_when(t.get("used_at"))
            expires_at = _fmt_when(t.get("expires_at"))
            error_line = f"\nError:       {t.get('error_msg') or ''}" if status == "error" else ""
            yield Static(
                f"Scope:       {t['scope']}\n"
                f"Status:      {status}\n"
                f"Enabled:     {enabled}\n"
                f"Last used:   {used_at}\n"
                f"Expires:     {expires_at}\n"
                f"Token limit: {limit}\n"
                f"Used (24h):  {t['tokens_used']:,}\n"
                f"Calls (24h): {t['call_count']}\n"
                f"Free:        {pct}"
                f"{error_line}"
            )


class CredentialsScreen(Screen):

    BINDINGS = [  # noqa: RUF012
        Binding("enter", "view_token", "Enter Detail", show=True),
        Binding("r", "reset_error", "r Clear Err", show=True),
        Binding("x", "deregister", "x Deregister", show=True),
        Binding("slash", "focus_search", "/ Search", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._tokens: list[dict] = []
        self._search_text: str = ""
        self._just_highlighted = False

    def compose(self) -> ComposeResult:
        yield Sidebar(active="credentials")
        with Vertical(classes="screen-content"):
            yield Input(placeholder="Search...", id="tokens-search")
            with Container(id="tokens-list-panel", classes="panel"):
                yield Static("Credentials", classes="panel-title")
                yield DataTable(id="tokens-table")
            with Container(id="token-detail-panel", classes="panel"):
                yield Static("Credential Detail", classes="panel-title")
                yield Static("Select a credential to view details", id="token-detail-content")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tokens-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "ID", "Type", "Label", "Status", "Last used",
            "Expires", "Used (24h)", "Free%", "Enabled",
        )
        self._load_data()

    def action_refresh(self) -> None:
        self._load_data()

    @work(thread=True)
    def _load_data(self) -> None:
        from ..data import get_credentials_with_usage

        tokens = get_credentials_with_usage(self.app.conn)
        self.app.call_from_thread(self._update_ui, tokens)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tokens-search":
            self._search_text = event.value.strip().lower()
            self._refresh_table()

    def action_focus_search(self) -> None:
        self.query_one("#tokens-search", Input).focus()

    def _update_ui(self, tokens: list[dict]) -> None:
        self._tokens = tokens
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#tokens-table", DataTable)
        table.clear()
        filtered = self._tokens
        if self._search_text:
            filtered = [
                t for t in filtered
                if self._search_text in " ".join(str(t.get(k, "") or "") for k in _CREDENTIAL_SEARCH_KEYS).lower()
            ]
        if filtered:
            for t in filtered:
                status = t.get("status", "ok")
                pct = f"{t['pct_free']:.1f}" if t["token_limit"] > 0 else "-"
                enabled = "yes" if t["enabled"] else "no"
                table.add_row(
                    str(t["id"]),
                    t["scope"],
                    t["label"],
                    status,
                    _fmt_when(t.get("used_at")),
                    _fmt_when(t.get("expires_at")),
                    str(t["tokens_used"]),
                    pct,
                    enabled,
                    key=str(t["id"]),
                )
        else:
            table.add_row("--", "--", "No credentials registered", "--", "--", "--", "--", "--", "--")
        self._update_detail_panel()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._just_highlighted:
            self._just_highlighted = False
            return
        self.action_view_token()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._just_highlighted = True
        self._update_detail_panel()

    def _update_detail_panel(self) -> None:
        token = self._get_selected_token()
        detail = self.query_one("#token-detail-content", Static)
        if token is None:
            detail.update("Select a credential to view details")
            return
        status = token.get("status", "ok")
        enabled = "yes" if token["enabled"] else "no"
        limit = str(token["token_limit"]) if token["token_limit"] > 0 else "unlimited"
        pct = f"{token['pct_free']:.1f}%" if token["token_limit"] > 0 else "-"
        used_at = _fmt_when(token.get("used_at"))
        expires_at = _fmt_when(token.get("expires_at"))
        error_line = (
            f"\nError:       {token.get('error_msg') or ''}" if status == "error" else ""
        )
        detail.update(
            f"ID:          {token['id']}\n"
            f"Scope:       {token['scope']}\n"
            f"Label:       {token['label']}\n"
            f"Status:      {status}\n"
            f"Enabled:     {enabled}\n"
            f"Last used:   {used_at}\n"
            f"Expires:     {expires_at}\n"
            f"Token limit: {limit}\n"
            f"Used (24h):  {token['tokens_used']:,}\n"
            f"Calls (24h): {token['call_count']}\n"
            f"Free:        {pct}"
            f"{error_line}"
        )

    def _get_selected_token(self) -> dict | None:
        if not self._tokens:
            return None
        table = self.query_one("#tokens-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return None
        row_data = table.get_row_at(table.cursor_row)
        token_id = row_data[0]
        for t in self._tokens:
            if str(t["id"]) == token_id:
                return t
        return None

    def action_view_token(self) -> None:
        token = self._get_selected_token()
        if token:
            self.app.push_screen(CredentialUsageScreen(token))

    def action_reset_error(self) -> None:
        token = self._get_selected_token()
        if not token:
            return
        if token.get("status") != "error":
            self.notify(f"Credential #{token['id']} is not in error state")
            return
        self.app.push_screen(
            ConfirmScreen(
                f"Clear error status on credential #{token['id']} ({token['label']})?"
            ),
            callback=lambda confirmed: self._do_reset_error(token) if confirmed else None,
        )

    @work(thread=True)
    def _do_reset_error(self, token: dict) -> None:
        from ..data import do_reset_credential_error

        do_reset_credential_error(self.app.conn, token["id"])
        self.app.call_from_thread(
            self.notify, f"Credential #{token['id']} error cleared"
        )
        self.app.call_from_thread(self._load_data)

    def action_deregister(self) -> None:
        token = self._get_selected_token()
        if not token:
            return
        self.app.push_screen(
            ConfirmScreen(
                f"Deregister credential #{token['id']} ({token['label']})? "
                f"This cannot be undone."
            ),
            callback=lambda confirmed: self._do_deregister(token) if confirmed else None,
        )

    @work(thread=True)
    def _do_deregister(self, token: dict) -> None:
        from ..data import do_deregister_credential

        do_deregister_credential(self.app.conn, token["id"])
        self.app.call_from_thread(
            self.notify, f"Credential #{token['id']} deregistered"
        )
        self.app.call_from_thread(self._load_data)
