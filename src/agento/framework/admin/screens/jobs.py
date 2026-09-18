from __future__ import annotations

import subprocess

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Static

from ..widgets.confirm import ConfirmScreen
from ..widgets.sidebar import Sidebar

_STATUS_CYCLE = [None, "TODO", "RUNNING", "SUCCESS", "FAILED", "DEAD"]


def _format_duration(started_at, finished_at) -> str:
    if not started_at or not finished_at:
        return "-"
    delta = finished_at - started_at
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "-"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


class JobsScreen(Screen):

    BINDINGS = [  # noqa: RUF012
        Binding("enter", "view_detail", "Enter Detail", show=True),
        Binding("p", "replay_job", "p Replay", show=True),
        Binding("slash", "focus_search", "/ Search", show=True),
        Binding("s", "cycle_status", "s Filter Status", show=True),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._status_index = 0
        self._search_text: str = ""
        self._jobs: list[dict] = []
        self._just_highlighted = False

    def compose(self) -> ComposeResult:
        yield Sidebar(active="jobs")
        with Vertical(classes="screen-content"):
            with Horizontal(id="jobs-filter-bar"):
                yield Input(placeholder="Search...", id="jobs-search")
                yield Static("Filter: All", id="jobs-status-label")
            with Vertical(id="jobs-table-panel", classes="panel"):
                yield Static("Jobs", classes="panel-title")
                yield DataTable(id="jobs-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "Type", "Status", "Agent View", "Reference", "Created", "Duration")
        self._load_data()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "jobs-search":
            self._search_text = event.value.strip().lower()
            self._refresh_table()

    def action_refresh(self) -> None:
        self._load_data()

    def action_focus_search(self) -> None:
        self.query_one("#jobs-search", Input).focus()

    def action_cycle_status(self) -> None:
        self._status_index = (self._status_index + 1) % len(_STATUS_CYCLE)
        current = _STATUS_CYCLE[self._status_index]
        label = current if current else "All"
        self.query_one("#jobs-status-label", Static).update(f"Filter: {label}")
        self._load_data()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._just_highlighted:
            self._just_highlighted = False
            return
        self.action_view_detail()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._just_highlighted = True

    def action_view_detail(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        row_data = table.get_row(row_key)
        job_id_str = str(row_data[0])
        if not job_id_str.isdigit():
            return
        self.app.push_screen(JobDetailScreen(int(job_id_str)))

    def action_replay_job(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        row_data = table.get_row(row_key)
        job_id_str = str(row_data[0])
        if not job_id_str.isdigit():
            return
        job_id = int(job_id_str)
        def _on_confirm(confirmed: bool) -> None:
            if confirmed:
                self._run_replay(job_id)

        self.app.push_screen(ConfirmScreen(f"Replay job #{job_id}?"), _on_confirm)

    @work(thread=True)
    def _load_data(self) -> None:
        from ..data import get_jobs

        status = _STATUS_CYCLE[self._status_index]
        jobs = get_jobs(self.app.conn, status=status)
        self.app.call_from_thread(self._update_jobs, jobs)

    def _update_jobs(self, jobs: list[dict]) -> None:
        self._jobs = jobs
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        table.clear()
        filtered = self._jobs
        if self._search_text:
            filtered = [
                j for j in filtered
                if self._search_text in " ".join(
                    str(j.get(k, "") or "") for k in ("id", "type", "status", "agent_view_code", "reference_id")
                ).lower()
            ]
        if filtered:
            for job in filtered:
                duration = _format_duration(job.get("started_at"), job.get("finished_at"))
                table.add_row(
                    str(job.get("id", "")),
                    str(job.get("type", "")),
                    str(job.get("status", "")),
                    str(job.get("agent_view_code", "") or ""),
                    str(job.get("reference_id", "") or ""),
                    str(job.get("created_at", "")),
                    duration,
                )
        else:
            table.add_row("--", "--", "No jobs found", "--", "--", "--", "--")

    @work(thread=True)
    def _run_replay(self, job_id: int) -> None:
        try:
            subprocess.run(
                ["/opt/cron-agent/run.sh", "replay", str(job_id)],
                capture_output=True, text=True, timeout=120,
            )
            self.app.call_from_thread(self.notify, f"Replay started for job #{job_id}")
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(self.notify, f"Replay timed out for job #{job_id}", severity="error")
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Replay failed: {e}", severity="error")


class JobDetailScreen(ModalScreen):

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "dismiss", "Esc Close", show=True),
    ]

    def __init__(self, job_id: int, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._job_id = job_id

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="job-detail-scroll", classes="panel"):
            yield Static(f"Job #{self._job_id}", classes="panel-title")
            yield Static("Loading...", id="job-detail-content")

    def on_mount(self) -> None:
        self._load_detail()

    @work(thread=True)
    def _load_detail(self) -> None:
        from ..data import get_job_detail

        detail = get_job_detail(self.app.conn, self._job_id)
        self.app.call_from_thread(self._update_detail, detail)

    def _update_detail(self, detail: dict | None) -> None:
        if not detail:
            self.query_one("#job-detail-content", Static).update("Job not found.")
            return

        lines = [
            f"ID:             {detail.get('id', '')}",
            f"Type:           {detail.get('type', '')}",
            f"Status:         {detail.get('status', '')}",
            f"Harness:        {detail.get('agent_type', '') or ''}",
            f"Model:          {detail.get('model', '') or ''}",
            f"Agent View:     {detail.get('agent_view_code', '') or ''}",
            f"Reference:      {detail.get('reference_id', '') or ''}",
            f"Created:        {detail.get('created_at', '')}",
            f"Started:        {detail.get('started_at', '') or '-'}",
            f"Finished:       {detail.get('finished_at', '') or '-'}",
            f"Duration:       {_format_duration(detail.get('started_at'), detail.get('finished_at'))}",
            f"Input Tokens:   {detail.get('input_tokens', '') or '-'}",
            f"Output Tokens:  {detail.get('output_tokens', '') or '-'}",
        ]

        error = detail.get("error_message")
        if error:
            lines.append(f"\nError:\n{error}")

        summary = detail.get("result_summary")
        if summary:
            lines.append(f"\nResult Summary:\n{summary}")

        prompt = detail.get("prompt")
        if prompt:
            truncated = prompt[:500]
            suffix = "..." if len(prompt) > 500 else ""
            lines.append(f"\nPrompt:\n{truncated}{suffix}")

        output = detail.get("output")
        if output:
            truncated = output[:500]
            suffix = "..." if len(output) > 500 else ""
            lines.append(f"\nOutput:\n{truncated}{suffix}")

        self.query_one("#job-detail-content", Static).update("\n".join(lines))


