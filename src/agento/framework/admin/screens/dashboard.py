from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ..widgets.sidebar import Sidebar


class DashboardScreen(Screen):

    def compose(self) -> ComposeResult:
        yield Sidebar(active="dashboard")
        with VerticalScroll(classes="screen-content"):
            with Horizontal(id="dashboard-top"):
                with Vertical(id="health-panel", classes="panel"):
                    yield Static("System Health", classes="panel-title")
                    yield Static("Loading...", id="health-content")
                with Vertical(id="system-panel", classes="panel"):
                    yield Static("System Info", classes="panel-title")
                    yield Static("Loading...", id="system-content")
            with Container(id="jobs-panel", classes="panel"):
                yield Static("Recent Jobs", classes="panel-title")
                yield DataTable(id="recent-jobs-table")
            with Horizontal(id="dashboard-bottom"):
                with Vertical(id="tokens-panel", classes="panel"):
                    yield Static("Credentials", classes="panel-title")
                    yield Static("Loading...", id="tokens-content")
                with Vertical(id="agents-panel", classes="panel"):
                    yield Static("Agent Views", classes="panel-title")
                    yield Static("Loading...", id="agents-content")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#recent-jobs-table", DataTable)
        table.add_columns("ID", "Type", "Status", "Reference", "Agent View", "Created")
        self._load_data()

    def action_refresh(self) -> None:
        self._load_data()

    @work(thread=True)
    def _load_data(self) -> None:
        from ..data import get_dashboard_data

        data = get_dashboard_data(self.app.conn)
        self.app.call_from_thread(self._update_ui, data)

    def _update_ui(self, data) -> None:
        # Health panel
        db_status = "Connected" if data.db_connected else "Disconnected"
        health_lines = [
            f"Database: {db_status}",
            f"Running jobs: {data.running_jobs}",
        ]
        self.query_one("#health-content", Static).update("\n".join(health_lines))

        # System panel
        system_lines = [
            f"Version: {data.version}",
            f"Python: {data.python_version}",
            f"Modules: {data.module_count}",
        ]
        self.query_one("#system-content", Static).update("\n".join(system_lines))

        # Recent jobs table
        table = self.query_one("#recent-jobs-table", DataTable)
        table.clear()
        if data.recent_jobs:
            for job in data.recent_jobs:
                table.add_row(
                    str(job.get("id", "")),
                    str(job.get("type", "")),
                    str(job.get("status", "")),
                    str(job.get("reference_id", "") or ""),
                    str(job.get("agent_view_code", "") or ""),
                    str(job.get("created_at", "")),
                )
        else:
            table.add_row("--", "--", "No jobs found", "--", "--", "--")

        # Tokens panel
        if data.tokens:
            token_lines = []
            for t in data.tokens:
                status = t.get("status", "ok")
                status_tag = " [error]" if status == "error" else ""
                enabled = "" if t.get("enabled") else " (disabled)"
                token_lines.append(f"{t['label']} ({t['scope']}){status_tag}{enabled}")
            self.query_one("#tokens-content", Static).update("\n".join(token_lines))
        else:
            self.query_one("#tokens-content", Static).update("No credentials registered")

        # Agent views panel
        if data.agent_views:
            av_lines = [f"{av['code']} ({av['label']})" for av in data.agent_views]
            self.query_one("#agents-content", Static).update("\n".join(av_lines))
        else:
            self.query_one("#agents-content", Static).update("No active agent views")
