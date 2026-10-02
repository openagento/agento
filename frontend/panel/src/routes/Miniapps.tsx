import { useEffect, useState } from "react";
import { notifications } from "@mantine/notifications";
import { apiFetch, ApiError, useQuery } from "@agento/api";
import { Button, DataTable, PageHeader, SectionHeader, SelectField, Timestamp, type Column } from "@agento/ui";
import { endLaunch, openLaunch, reconcileLaunches } from "../launch";

interface AgentView { id: number; code: string; label: string; workspace_id: number }
interface Miniapp { artifact_code: string; version_id: string; title: string }
interface Launch { launch_id: string; artifact_code: string; version_id: string; agent_view_id: number; expires_at: string }

const message = (e: unknown) => (e instanceof ApiError ? e.message : "The request failed.");

export function Miniapps() {
  const views = useQuery({ queryKey: ["agent-views"], queryFn: ({ signal }) => apiFetch<AgentView[]>("/api/agent-views", { signal }) });
  const [picked, setPicked] = useState<string>("");
  const viewId = Number(picked || views.data?.[0]?.id || 0);
  const apps = useQuery({
    queryKey: ["miniapps", viewId],
    enabled: viewId > 0,
    queryFn: ({ signal }) => apiFetch<Miniapp[]>(`/api/agent-views/${viewId}/miniapps`, { signal }),
  });
  const launches = useQuery({
    queryKey: ["launches"],
    queryFn: async ({ signal }) => {
      const requestedAt = Date.now();
      return { requestedAt, rows: await apiFetch<Launch[]>("/api/launches", { signal }) };
    },
  });
  const live = new Set((launches.data?.rows ?? []).map((l) => l.launch_id));
  const listed = launches.data;
  useEffect(() => {
    if (listed) reconcileLaunches(new Set(listed.rows.map((l) => l.launch_id)), listed.requestedAt);
  }, [listed]);

  const open = (m: Miniapp) => {
    // No await before openLaunch: it reserves the window inside this click.
    openLaunch({ agentViewId: viewId, artifactCode: m.artifact_code }, live)
      .then((r) => {
        if (r.kind === "blocked") notifications.show({ color: "red", message: "The browser blocked the window. Allow pop-ups for the panel and try again." });
      })
      .catch((e) => notifications.show({ color: "red", message: message(e) }));
  };

  const appColumns: Column<Miniapp>[] = [
    { id: "title", header: "Miniapp", cell: (m) => m.title, sortValue: (m) => m.title },
    { id: "code", header: "Code", cell: (m) => <code>{m.artifact_code}</code>, sortValue: (m) => m.artifact_code },
    { id: "open", header: "Actions", cell: (m) => <Button variant="primary" onClick={() => open(m)}>Open</Button> },
  ];
  const launchColumns: Column<Launch>[] = [
    { id: "code", header: "Miniapp", cell: (l) => <code>{l.artifact_code}</code>, sortValue: (l) => l.artifact_code },
    { id: "expires", header: "Expires", cell: (l) => <Timestamp value={l.expires_at} />, sortValue: (l) => l.expires_at },
    { id: "end", header: "Actions", cell: (l) => (
      <Button variant="danger" onClick={() => endLaunch(l.launch_id).catch((e) => notifications.show({ color: "red", message: message(e) }))}>
        End
      </Button>
    ) },
  ];

  return (
    <div className="ag-stack">
      <PageHeader title="Miniapps" description="Apps an agent published. Each one opens in its own window." />
      {(views.data?.length ?? 0) > 1 && (
        <SelectField label="Agent view" name="agent-view" value={String(viewId)} onChange={setPicked}
          options={(views.data ?? []).map((v) => ({ value: String(v.id), label: v.label || v.code }))} />
      )}
      <DataTable caption="Miniapps" columns={appColumns} rows={viewId > 0 ? apps.data : []} rowKey={(m) => m.artifact_code}
        loading={views.isPending || (viewId > 0 && apps.isPending)}
        error={views.error ? message(views.error) : apps.error ? message(apps.error) : null}
        onRetry={() => void (views.error ? views.refetch() : apps.refetch())}
        emptyTitle="No miniapps" emptyText="A miniapp shows here when this agent view has one you may open." />
      <SectionHeader>Open launches</SectionHeader>
      <p className="ag-muted">Opening more launches than the limit (web/launch/max_concurrent) ends the oldest one.</p>
      <DataTable caption="Open launches" columns={launchColumns} rows={launches.data?.rows} rowKey={(l) => l.launch_id}
        loading={launches.isPending} error={launches.error ? message(launches.error) : null}
        onRetry={() => void launches.refetch()} emptyTitle="No open launches" />
    </div>
  );
}
