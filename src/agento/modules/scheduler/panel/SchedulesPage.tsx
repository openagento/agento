// The schedules a user may read, by group (PRD §11). The API scopes the rows and sends each
// row's rights (`can`); this page only hides what the user cannot do.
import { useState } from "react";
import { useNavigate } from "react-router";
import { apiFetch, useErrorText, useMutation, useQueryClient } from "@agento/api";
import {
  AppLink, Button, DataTable, EmptyState, PageHeader, RowMenu, SectionHeader, StatusBadge, SwitchField, Timestamp, jobTone, useJobStatusText, useT,
  type Column, type Translate,
} from "@agento/ui";
import { scheduleUrl, useExplain, useOptions, useSchedules, type Group, type Schedule } from "./api";

const groupTitles = (t: Translate): [Group, string][] => [
  ["mine", t("list.group.mine")], ["shared", t("list.group.shared")], ["agent", t("list.group.agent")], ["user", t("list.group.user")],
];

export default function SchedulesPage() {
  const list = useSchedules();
  const options = useOptions();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const t = useT("scheduler");
  const statusText = useJobStatusText();
  const errorText = useErrorText("scheduler");
  const explain = useExplain();
  const [failure, setFailure] = useState<unknown>(null);
  const toggle = useMutation({
    mutationFn: (s: Schedule) => apiFetch(scheduleUrl(s.id), { method: "PATCH", json: { enabled: !s.enabled } }),
    onSuccess: () => setFailure(null),
    onError: (e) => setFailure(e),
    onSettled: () => qc.invalidateQueries({ queryKey: ["scheduler"] }),
  });
  const canCreate = (options.data?.agent_views.length ?? 0) > 0;

  const columns: Column<Schedule>[] = [
    { id: "title", header: t("list.column.title"), sortValue: (s) => s.title, cell: (s) => <AppLink to={`/schedules/${s.id}`}>{s.title}</AppLink> },
    { id: "view", header: t("list.column.view"), cell: (s) => s.agent_view_code ?? t("list.no_view") },
    { id: "owner", header: t("list.column.owner"), cell: (s) => s.owner_username ?? t("list.owner_agent") },
    // The preset label, or "Custom" (the server sends the RRULE itself when no preset matches, D-17).
    { id: "recurrence", header: t("list.column.recurrence"), cell: (s) => (!s.recurrence_label || s.recurrence_label === s.rrule
      ? t("recurrence.custom") : s.recurrence_label) },
    { id: "next", header: t("list.column.next"), cell: (s) => (s.last_error ? <span className="ag-field__error">{explain(s.last_error)}</span>
      : !s.enabled ? <span className="ag-muted">{t("list.paused")}</span>
        : s.next_fire_at ? <Timestamp value={s.next_fire_at} /> : <span className="ag-muted">{t("list.no_next")}</span>) },
    { id: "last", header: t("list.column.last"), nowrap: true, cell: (s) => (s.last_run_status
      ? <StatusBadge tone={jobTone(s.last_run_status)}>{statusText(s.last_run_status)}</StatusBadge> : <span className="ag-muted">{t("list.never")}</span>) },
    { id: "enabled", header: t("list.column.enabled"), cell: (s) => (
      <SwitchField hideLabel label={t("list.enabled_of", { title: s.title })} checked={toggle.isPending && toggle.variables.id === s.id ? !s.enabled : s.enabled}
        disabled={!s.can.write || toggle.isPending} onChange={() => toggle.mutate(s)} />
    ) },
    { id: "actions", header: t("list.column.actions"), cell: (s) => <RowMenu actions={[
      { id: "open", label: s.can.write ? t("list.action.edit") : t("list.action.view"), icon: "rename", onClick: () => navigate(`/schedules/${s.id}`) },
      ...(s.conversation_id ? [{ id: "open_conversation", label: t("action.open_conversation"), onClick: () => navigate(`/conversations/${s.conversation_id}`) }] : []),
    ]} /> },
  ];

  const groups = list.data?.groups;
  const shown = groupTitles(t).filter(([key]) => groups?.[key]?.length || (key === "mine" && canCreate));
  return (
    <div className="ag-stack">
      <PageHeader title={t("list.title")} description={t("list.description")}
        actions={canCreate && <Button variant="primary" onClick={() => navigate("/schedules/new")}>{t("list.new")}</Button>} />
      {failure !== null && <p className="ag-field__error" role="alert">{errorText(failure, "error.generic")}</p>}
      {!groups ? (
        <DataTable caption={t("list.title")} columns={columns} rows={undefined} rowKey={(s) => String(s.id)}
          loading={list.isPending} error={list.error ? errorText(list.error, "error.generic") : null} onRetry={() => void list.refetch()} />
      ) : shown.length === 0 ? <EmptyState title={t("list.empty.title")}>{t("list.empty.none_shared")}</EmptyState>
        : shown.map(([key, title]) => (
        <section key={key} className="ag-stack">
          <SectionHeader>{title}</SectionHeader>
          <DataTable caption={title} columns={columns} rows={groups[key]} rowKey={(s) => String(s.id)}
            emptyTitle={t("list.empty.title")} emptyText={t("list.empty.create")} />
        </section>
      ))}
    </div>
  );
}
