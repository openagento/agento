// The schedules a user may read, by group (PRD §11). The API scopes the rows and sends each
// row's rights (`can`); this page only hides what the user cannot do.
import { useState } from "react";
import { useNavigate } from "react-router";
import { apiFetch, useMutation, useQueryClient } from "@agento/api";
import {
  AppLink, Button, DataTable, EmptyState, PageHeader, RowMenu, SectionHeader, StatusBadge, SwitchField, Timestamp, jobTone, type Column,
} from "@agento/ui";
import { explain, message, scheduleUrl, useOptions, useSchedules, type Group, type Schedule } from "./api";

const GROUPS: [Group, string][] = [
  ["mine", "My schedules"], ["shared", "Shared with me"], ["agent", "Agent schedules"], ["user", "Other users' schedules"],
];

/** The preset label, or "Custom" (the server sends the RRULE itself when no preset matches, D-17). */
export const recurrence = (s: Schedule) => (!s.recurrence_label || s.recurrence_label === s.rrule ? "Custom" : s.recurrence_label);

export default function SchedulesPage() {
  const list = useSchedules();
  const options = useOptions();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [failure, setFailure] = useState<string | null>(null);
  const toggle = useMutation({
    mutationFn: (s: Schedule) => apiFetch(scheduleUrl(s.id), { method: "PATCH", json: { enabled: !s.enabled } }),
    onSuccess: () => setFailure(null),
    onError: (e) => setFailure(message(e)),
    onSettled: () => qc.invalidateQueries({ queryKey: ["scheduler"] }),
  });
  const canCreate = (options.data?.agent_views.length ?? 0) > 0;

  const columns: Column<Schedule>[] = [
    { id: "title", header: "Title", sortValue: (s) => s.title, cell: (s) => <AppLink to={`/schedules/${s.id}`}>{s.title}</AppLink> },
    { id: "view", header: "Agent view", cell: (s) => s.agent_view_code ?? "none" },
    { id: "owner", header: "Owner", cell: (s) => s.owner_username ?? "agent" },
    { id: "recurrence", header: "Recurrence", cell: recurrence },
    { id: "next", header: "Next run", cell: (s) => (s.last_error ? <span className="ag-field__error">{explain(s.last_error)}</span>
      : !s.enabled ? <span className="ag-muted">Paused</span>
        : s.next_fire_at ? <Timestamp value={s.next_fire_at} /> : <span className="ag-muted">None</span>) },
    { id: "last", header: "Last run", nowrap: true, cell: (s) => (s.last_run_status
      ? <StatusBadge tone={jobTone(s.last_run_status)}>{s.last_run_status}</StatusBadge> : <span className="ag-muted">Never</span>) },
    { id: "enabled", header: "Enabled", cell: (s) => (
      <SwitchField hideLabel label={`Enabled: ${s.title}`} checked={toggle.isPending && toggle.variables.id === s.id ? !s.enabled : s.enabled}
        disabled={!s.can.write || toggle.isPending} onChange={() => toggle.mutate(s)} />
    ) },
    { id: "actions", header: "Actions", cell: (s) => <RowMenu actions={[
      { label: s.can.write ? "Edit" : "View", icon: "rename", onClick: () => navigate(`/schedules/${s.id}`) },
      ...(s.conversation_id ? [{ label: "Open conversation", onClick: () => navigate(`/conversations/${s.conversation_id}`) }] : []),
    ]} /> },
  ];

  const groups = list.data?.groups;
  const shown = GROUPS.filter(([key]) => groups?.[key]?.length || (key === "mine" && canCreate));
  return (
    <div className="ag-stack">
      <PageHeader title="Schedules" description="Prompts that run an agent on a recurrence."
        actions={canCreate && <Button variant="primary" onClick={() => navigate("/schedules/new")}>New schedule</Button>} />
      {failure && <p className="ag-field__error" role="alert">{failure}</p>}
      {!groups ? (
        <DataTable caption="Schedules" columns={columns} rows={undefined} rowKey={(s) => String(s.id)}
          loading={list.isPending} error={list.error ? message(list.error) : null} onRetry={() => void list.refetch()} />
      ) : shown.length === 0 ? <EmptyState title="No schedules">No schedule is shared with you.</EmptyState>
        : shown.map(([key, title]) => (
        <section key={key} className="ag-stack">
          <SectionHeader>{title}</SectionHeader>
          <DataTable caption={title} columns={columns} rows={groups[key]} rowKey={(s) => String(s.id)}
            emptyTitle="No schedules" emptyText="Create one with New schedule." />
        </section>
      ))}
    </div>
  );
}
