from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from agento.modules.jira.src.toolbox_client import ToolboxAPIError, ToolboxClient

from .onboarding import (
    _create_field,
    _create_status,
    _find_field,
    _find_status,
    _try_screen_mapping,
)

# The Frequency select field is resolved by NAME (config stores only its id, which can
# drift). See plan C2.
FREQUENCY_FIELD_NAME = "Frequency"

# Jira project keys are a restricted charset; validating up front turns a would-be 400
# (or JQL/path injection) into a clear per-project error. See plan C7.
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,254}$")


def _valid_project_key(key: str) -> bool:
    return bool(_PROJECT_KEY_RE.match(key or ""))


def _jql_escape(value: str) -> str:
    """Escape a value for embedding inside a JQL double-quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _resolve_global_context(
    toolbox: ToolboxClient, field_id: str, admin_auth: dict | None,
) -> str | None:
    """Return the field's global context id (else the first), or None if none exist.

    A select field's options live on its (normally single, global) context — there is
    no per-project option context (plan C1).
    """
    auth_kw = admin_auth or {}
    data = toolbox.jira_request("GET", f"/rest/api/3/field/{field_id}/context", **auth_kw)
    contexts = data.get("values", []) if isinstance(data, dict) else []
    if not contexts:
        return None
    for ctx in contexts:
        if ctx.get("isGlobalContext"):
            return ctx["id"]
    return contexts[0]["id"]


def _context_option_values(
    toolbox: ToolboxClient, field_id: str, context_id: str, admin_auth: dict | None,
) -> set[str]:
    """Read the current option values on a field context (read-only helper)."""
    auth_kw = admin_auth or {}
    data = toolbox.jira_request(
        "GET", f"/rest/api/3/field/{field_id}/context/{context_id}/option", **auth_kw
    )
    values = data.get("values", []) if isinstance(data, dict) else []
    return {o["value"] for o in values if "value" in o}


@dataclass
class ProjectResult:
    key: str
    status_ok: bool = False
    status_created: bool = False
    issues_scanned: int = 0
    unresolvable: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False
    incomplete: bool = False
    team_managed: bool = False
    field_associated: bool = False
    error: str | None = None
    inconsistent: bool = False
    failed: bool = False


@dataclass
class ConfigureReport:
    status_name: str = ""
    resolved_field_id: str | None = None
    field_created: bool = False
    field_drift: bool = False
    options_checked: bool = False
    missing_options: list[str] = field(default_factory=list)
    extra_options: list[str] = field(default_factory=list)
    options_added: list[str] = field(default_factory=list)
    projects: list[ProjectResult] = field(default_factory=list)
    inconsistent: bool = False
    incomplete: bool = False
    options_incomplete: bool = False
    options_need_admin: bool = False
    failed: bool = False


class JiraPeriodicConfigurer:
    """Apply or check the Jira periodic-task status + Frequency field across projects.

    DB-free and config-resolution-free: it receives already-resolved plain data plus an
    injected ``ToolboxClient`` (mirrors ``JiraCronSync``), so it is unit-testable in
    isolation. ``admin_auth`` is the operator setup credential (plan C6); every read is
    performed through the toolbox.
    """

    def __init__(
        self,
        toolbox: ToolboxClient,
        *,
        projects: list[str],
        status_name: str,
        field_id: str | None,
        frequency_map: dict[str, str],
        admin_auth: dict | None,
        logger: logging.Logger,
        check: bool,
    ):
        self.toolbox = toolbox
        self.projects = projects
        self.status_name = status_name
        self.field_id = field_id
        self.frequency_map = frequency_map
        self.admin_auth = admin_auth
        self.logger = logger
        self.check = check

    def run(self) -> ConfigureReport:
        report = ConfigureReport(status_name=self.status_name)
        self._resolve_field(report)
        # Short-circuit: without a field id there are no options to diff and no issue
        # scan is possible (never call jira_search with fields=[None]). Field-missing is
        # already recorded as inconsistent/failed.
        if report.resolved_field_id:
            self._diff_options(report)
        for key in dict.fromkeys(self.projects):
            pr = self._configure_project(key, report.resolved_field_id)
            report.projects.append(pr)
            report.inconsistent = report.inconsistent or pr.inconsistent
            report.failed = report.failed or pr.failed
            report.incomplete = report.incomplete or pr.incomplete
        return report

    def _field_exists_by_id(self, field_id: str) -> bool:
        """True if a field with this id still exists (its context is readable)."""
        try:
            self.toolbox.jira_request(
                "GET", f"/rest/api/3/field/{field_id}/context", **(self.admin_auth or {})
            )
            return True
        except ToolboxAPIError:
            return False

    def _resolve_field(self, report: ConfigureReport) -> None:
        found = _find_field(self.toolbox, FREQUENCY_FIELD_NAME)
        if found is None and self.field_id and self._field_exists_by_id(self.field_id):
            # Jira's GET /field list can lag (or not reflect) a just-created custom field; fall
            # back to the persisted id so a re-run reuses it instead of creating a duplicate (C9).
            found = self.field_id
        if found is None:
            if self.check:
                report.inconsistent = True
                return
            # apply: create it (needs admin)
            if self.admin_auth is None:
                report.failed = True
                self.logger.error("Frequency field missing and no admin token to create it")
                return
            created = _create_field(self.toolbox, FREQUENCY_FIELD_NAME, self.admin_auth)
            if not created:
                report.failed = True
                return
            report.resolved_field_id = created
            report.field_created = True
            return
        report.resolved_field_id = found
        if self.field_id and found != self.field_id:
            report.field_drift = True
            if self.check:
                # sync reads the STORED id — a stale id silently skips every issue, so a
                # read-only check must not report it clean (apply auto-corrects by persisting).
                report.inconsistent = True

    def _mark_options_incomplete(self, report: ConfigureReport, *, need_admin: bool = False) -> None:
        # Options could not be verified — distinct from a per-project issue-scan incompleteness so
        # the report attributes the cause correctly (R9). Also gates the check exit non-zero (F4).
        report.incomplete = True
        report.options_incomplete = True
        if need_admin:
            report.options_need_admin = True

    def _diff_options(self, report: ConfigureReport) -> None:
        if not self.frequency_map:
            self.logger.warning("frequency_map is empty — no options to sync")
            return
        if self.admin_auth is None:
            self._mark_options_incomplete(report, need_admin=True)
            self.logger.warning("options NOT verified — admin token required")
            return
        try:
            ctx = _resolve_global_context(self.toolbox, report.resolved_field_id, self.admin_auth)
        except ToolboxAPIError as e:
            self._mark_options_incomplete(report)
            self.logger.warning("Could not read field contexts: %s", e)
            return
        if not ctx:
            self._mark_options_incomplete(report)
            self.logger.warning("No field context found — cannot verify options")
            return
        try:
            existing = _context_option_values(
                self.toolbox, report.resolved_field_id, ctx, self.admin_auth
            )
        except ToolboxAPIError as e:
            self._mark_options_incomplete(report)
            self.logger.warning("Could not read field options: %s", e)
            return

        report.missing_options = [k for k in self.frequency_map if k not in existing]
        report.extra_options = sorted(v for v in existing if v not in self.frequency_map)
        report.options_checked = True

        if self.check:
            # Two-way: an extra option (in Jira, absent from frequency_map) also breaks the
            # mapping — an issue set to it is silently skipped by sync. apply never deletes
            # options (that would be destructive), so extra is a human-reconcile signal, but
            # the check must still flag it.
            if report.missing_options or report.extra_options:
                report.inconsistent = True
            return

        # apply
        if not report.missing_options:
            self.logger.info("options already in sync")
            return
        try:
            self.toolbox.jira_request(
                "POST",
                f"/rest/api/3/field/{report.resolved_field_id}/context/{ctx}/option",
                {"options": [{"value": v} for v in report.missing_options]},
                **self.admin_auth,
            )
            report.options_added = list(report.missing_options)
            self.logger.info("added option(s): %s", ", ".join(report.options_added))
        except ToolboxAPIError as e:
            report.failed = True
            self.logger.error("Failed to add options: %s", e)

    def _configure_project(self, key: str, field_id: str | None) -> ProjectResult:
        pr = ProjectResult(key=key)
        if not _valid_project_key(key):
            pr.error = "invalid project key"
            self._mark_project_bad(pr)
            return pr

        try:
            project = self.toolbox.jira_request("GET", f"/rest/api/3/project/{key}")
        except ToolboxAPIError as e:
            pr.error = f"project {e.status_code}"
            self._mark_project_bad(pr)
            return pr
        # Team-managed (next-gen) projects manage their field layout separately, so a company-managed
        # field isn't surfaced on their issues automatically — flag it so the report guides the
        # operator to add it in the project's Fields settings (C11).
        pr.team_managed = isinstance(project, dict) and project.get("style") == "next-gen"
        project_id = project.get("id") if isinstance(project, dict) else None

        status_id = _find_status(self.toolbox, key, self.status_name)
        if status_id:
            pr.status_ok = True
        elif self.check:
            pr.inconsistent = True
        else:
            created = _create_status(self.toolbox, key, self.status_name, self.admin_auth)
            if created:
                pr.status_ok = True
                pr.status_created = True
            else:
                pr.failed = True

        if not self.check and field_id:
            # Surface the field on the project (best-effort, never fatal).
            if pr.team_managed:
                # C12: team-managed projects need a field↔project association so the global field
                # becomes selectable in the project (the operator still adds it to the work type).
                if self.admin_auth and project_id:
                    self._associate_field_with_project(pr, field_id, project_id)
            else:
                # company-managed: add the field to the default screen.
                _try_screen_mapping(self.toolbox, key, field_id, self.logger, self.admin_auth)

        if field_id:
            self._scan_unresolvable(pr, key, field_id)

        return pr

    def _associate_field_with_project(self, pr: ProjectResult, field_id: str, project_id: str) -> None:
        try:
            self.toolbox.jira_request(
                "PUT",
                "/rest/api/3/field/association",
                {
                    "associationContexts": [{"type": "PROJECT_ID", "identifier": int(project_id)}],
                    "fields": [{"type": "FIELD_ID", "identifier": field_id}],
                },
                **self.admin_auth,
            )
            pr.field_associated = True
            self.logger.info("Associated field %s with team-managed project %s", field_id, pr.key)
        except (ToolboxAPIError, ValueError, TypeError) as e:
            self.logger.warning("Could not associate field with team-managed project %s: %s", pr.key, e)

    def _mark_project_bad(self, pr: ProjectResult) -> None:
        if self.check:
            pr.inconsistent = True
        else:
            pr.failed = True

    def _scan_unresolvable(self, pr: ProjectResult, key: str, field_id: str) -> None:
        jql = f'project = {key} AND status = "{_jql_escape(self.status_name)}"'
        try:
            resp = self.toolbox.jira_search(jql=jql, fields=[field_id], max_results=100)
        except ToolboxAPIError as e:
            self.logger.warning("Issue scan failed for %s: %s", key, e)
            pr.incomplete = True  # could not verify issues → check must not report clean
            return
        issues = resp.get("issues", [])
        pr.issues_scanned = len(issues)
        if len(issues) == 100:
            pr.truncated = True
            pr.incomplete = True  # only saw the first page — unresolvable issues may exist beyond
            self.logger.warning("Issue scan for %s may be truncated (>=100 issues)", key)
        for issue in issues:
            issue_key = issue.get("key", "?")
            freq_obj = issue.get("fields", {}).get(field_id)
            if freq_obj is None:
                pr.unresolvable.append((issue_key, "no value"))
                continue
            value = freq_obj.get("value") if isinstance(freq_obj, dict) else None
            if not value:
                pr.unresolvable.append((issue_key, "no value"))
            elif value not in self.frequency_map:
                pr.unresolvable.append((issue_key, "unknown"))
        if pr.unresolvable and self.check:
            pr.inconsistent = True


_UNRESOLVED_REASON = {
    "no value": "no Frequency value set",
    "unknown": "its Frequency value isn't one of the frequency_map options",
}


def _plain_project_error(err: str) -> str:
    if err == "invalid project key":
        return "invalid project key — rejected before querying Jira"
    if err.startswith("project "):
        return f"project not found or not accessible (Jira HTTP {err.split()[-1]})"
    return err


def render_report(report: ConfigureReport, *, check: bool) -> str:
    """Render a plain-language, sectioned report of the check/apply run."""
    lines: list[str] = []
    title = f"Jira periodic-tasks — {'checking' if check else 'applying'} configuration"
    lines.append(title)
    lines.append("═" * len(title))

    # ── The Frequency custom field + its dropdown options ──────────────────
    lines.append("")
    if report.resolved_field_id:
        state = "created" if report.field_created else "found"
        lines.append(f'Frequency field  ✓ {state}: "{FREQUENCY_FIELD_NAME}" ({report.resolved_field_id})')
        if report.field_drift:
            lines.append("                 ⚠ config points to a different field id — the scheduler "
                         "reads the stored id")
            lines.append("                   " + (
                f"(updated it to {report.resolved_field_id})" if not check
                else f"→ run: agento config:set jira_periodic_tasks/jira_frequency_field {report.resolved_field_id}"))
    else:
        lines.append(f'Frequency field  ✗ not found (no field named "{FREQUENCY_FIELD_NAME}")')
        lines.append("                 " + (
            "→ run apply (without --check) with an admin token to create it"
            if check else "→ could not create it — an admin token is required"))

    if report.options_added:
        lines.append(f"  Dropdown options  ✓ added {len(report.options_added)}: "
                     f"{', '.join(report.options_added)}")
        if report.extra_options:
            lines.append(f"                    ⚠ also in Jira but not in frequency_map: "
                         f"{', '.join(report.extra_options)}")
    elif report.options_checked:
        if not report.missing_options and not report.extra_options:
            lines.append("  Dropdown options  ✓ in sync with frequency_map")
        else:
            if report.missing_options:
                lines.append(f"  Dropdown options  ✗ missing from Jira: {', '.join(report.missing_options)} "
                             f"(run apply to add)")
            if report.extra_options:
                lines.append(f"                    ⚠ in Jira but not in frequency_map: "
                             f"{', '.join(report.extra_options)}")
    elif report.options_incomplete:
        reason = ("no admin token — the dropdown options could not be read"
                  if report.options_need_admin
                  else 'options could not be read; this tool syncs options for the global '
                       '"Frequency" field')
        lines.append(f"  Dropdown options  ? not verified — {reason}")

    # ── Per project ────────────────────────────────────────────────────────
    lines.append("")
    lines.append("Projects")
    for pr in report.projects:
        kind = " · team-managed" if pr.team_managed else ""
        lines.append(f"  {pr.key}{kind}")
        if pr.error:
            lines.append(f"    ✗ {_plain_project_error(pr.error)}")
            continue

        if pr.status_created:
            lines.append(f'    Status "{report.status_name}"   ✓ created')
        elif pr.status_ok:
            lines.append(f'    Status "{report.status_name}"   ✓ present')
        else:
            hint = "run apply to create it" if check else "could not be created (needs admin)"
            lines.append(f'    Status "{report.status_name}"   ✗ missing — {hint}')

        if pr.incomplete and not pr.unresolvable:
            note = "only the first 100 were checked" if pr.truncated else "the scan could not complete"
            lines.append(f"    Recurring issues  ? not fully checked — {note}")
        elif pr.unresolvable:
            tail = " (only the first 100 checked)" if pr.truncated else ""
            lines.append(f"    Recurring issues  ⚠ {pr.issues_scanned} in this status{tail}; "
                         f"{len(pr.unresolvable)} would be SKIPPED by the scheduler:")
            for k, reason in pr.unresolvable:
                lines.append(f"        • {k} — {_UNRESOLVED_REASON.get(reason, reason)}")
        elif pr.issues_scanned:
            lines.append(f"    Recurring issues  ✓ {pr.issues_scanned} in this status, all valid")

        if pr.team_managed and report.resolved_field_id:
            if pr.field_associated:
                lines.append(f'    → team-managed: the "{FREQUENCY_FIELD_NAME}" field is now available to '
                             f"this project.")
                lines.append("      Add it to the work type in Project settings → Fields so it shows on "
                             "issues (its options stay in sync automatically).")
            else:
                lines.append(f'    → team-managed: add the "{FREQUENCY_FIELD_NAME}" field in Project '
                             f"settings → Fields so it shows on issues (its options stay in sync).")

    # ── Summary ─────────────────────────────────────────────────────────────
    lines.append("")
    n = len(report.projects)
    if check:
        bad = sum(1 for pr in report.projects if pr.inconsistent)
        if report.inconsistent or report.incomplete:
            # Cover BOTH global problems (field / options) and per-project ones, so the result
            # line always states a concrete reason when the command exits non-zero.
            parts = []
            if not report.resolved_field_id:
                parts.append("Frequency field missing")
            elif report.field_drift:
                parts.append("stored field id is stale")
            if report.missing_options or report.extra_options:
                parts.append("dropdown options out of sync")
            if bad:
                parts.append(f"{bad} project{'s' if bad != 1 else ''} "
                             f"{'need' if bad != 1 else 'needs'} attention")
            if report.incomplete:
                parts.append("options not verified" if report.options_incomplete else "issue scan incomplete")
            lines.append(f"Result  ⚠ checked {n} project(s) — {', '.join(parts)}. "
                         f"(command exits non-zero)")
        else:
            lines.append(f"Result  ✓ checked {n} project(s) — everything is in order.")
    else:
        bad = sum(1 for pr in report.projects if pr.failed)
        if report.failed:
            lines.append(f"Result  ✗ configured {n} project(s) — {bad} failed (see above).")
        else:
            lines.append(f"Result  ✓ configured {n} project(s) — nothing failed.")

    return "\n".join(lines)
