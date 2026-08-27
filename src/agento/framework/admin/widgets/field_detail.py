from __future__ import annotations

from textual.widgets import Static


class FieldDetailPanel(Static):

    def __init__(self, **kwargs) -> None:
        super().__init__("Select a field to view details", **kwargs)
        # (path, scope, scope_id) -> "LABEL — message". Keyed by SCOPE as well as
        # path: the same field holds a different credential per scope, so showing
        # a default-scope result under an agent_view is a wrong answer, not a
        # stale one.
        self._results: dict[tuple[str, str, int], str] = {}
        self._scope: tuple[str, int] = ("default", 0)

    def set_scope(self, scope: str, scope_id: int) -> None:
        """The scope the panel is currently rendering for."""
        self._scope = (scope, scope_id)

    def set_test_result(self, path: str, line: str, scope: str, scope_id: int) -> None:
        """Record a test outcome so it survives the toast fading."""
        self._results[(path, scope, scope_id)] = line

    def update_field(self, field) -> None:
        if field is None:
            self.update("Select a field to view details")
            return

        value_display = "****" if field.obscure else (field.value if field.value is not None else "")

        lines = [
            f"Path:   {field.path}",
            f"Label:  {field.label}",
            f"Type:   {field.field_type}",
            f"Source: {field.source}",
            f"Value:  {value_display}",
        ]

        if field.options:
            opts = ", ".join(f"{o['value']} ({o['label']})" for o in field.options)
            lines.append(f"Options: {opts}")

        if not field.editable_at_scope:
            allowed = ", ".join(field.allowed_scopes) or "none"
            lines.append(f"Scopes: {allowed} (read-only here)")

        if field.source == "env":
            lines.append("")
            lines.append("This value is set via environment variable")
        elif field.source == "db:inherited":
            lines.append("")
            lines.append("This value is inherited from a parent scope")

        if getattr(field, "tester", ""):
            lines.append("")
            lines.append(f"Test:   press 't' to run the {field.tester} test")
            last = self._results.get((field.path, *self._scope))
            if last:
                lines.append(f"Last:   {last}")

        self.update("\n".join(lines))
