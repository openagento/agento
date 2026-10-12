// The apps catalogue (moved from the core panel): the apps approved for a view, and the open launches.
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { apiFetch, endLaunch, openLaunch, reconcileLaunches, useCan, useQuery } from "@agento/api";
import { DataTable, EndAction, OpenAction, PageHeader, SectionHeader, StatusBadge, Timestamp, type Column } from "@agento/ui";
import { message, useViews, ViewPicker } from "./common";

interface App { artifact_code: string; version_id: string; title: string; current_version: string | null; update_waiting: boolean }
interface Launch { launch_id: string; artifact_code: string; version_id: string; agent_view_id: number; expires_at: string }

export default function AppsPage() {
  const approver = useCan("miniapps.approve");
  const views = useViews();
  const [picked, setPicked] = useState("");
  const viewId = Number(picked || views.data?.[0]?.id || 0);
  const apps = useQuery({
    queryKey: ["miniapps", "catalogue", viewId],
    enabled: viewId > 0,
    queryFn: ({ signal }) => apiFetch<App[]>(`/api/miniapps/agent-views/${viewId}/catalogue`, { signal }),
  });
  const launches = useQuery({
    queryKey: ["launches"],
    queryFn: async ({ signal }) => {
      const requestedAt = Date.now();
      return { requestedAt, rows: await apiFetch<Launch[]>("/api/launches", { signal }) };
    },
  });
  const [failure, setFailure] = useState<string | null>(null);
  const live = new Set((launches.data?.rows ?? []).map((l) => l.launch_id));
  const listed = launches.data;
  useEffect(() => {
    if (listed) reconcileLaunches(new Set(listed.rows.map((l) => l.launch_id)), listed.requestedAt);
  }, [listed]);

  const open = (a: App) => {
    setFailure(null);
    // No await before openLaunch: it reserves the window inside this click.
    openLaunch({ agentViewId: viewId, artifactCode: a.artifact_code }, live)
      .then((r) => { if (r.kind === "blocked") setFailure("The browser blocked the window. Allow pop-ups for the panel and try again."); })
      .catch((e) => setFailure(message(e)));
  };

  const appColumns: Column<App>[] = [
    { id: "title", header: "App", sortValue: (a) => a.title,
      cell: (a) => (approver ? <Link to={`/apps/${a.artifact_code}?view=${viewId}`}>{a.title}</Link> : a.title) },
    { id: "version", header: "Version", cell: (a) => (
      <span className="ag-row"><code>{a.version_id}</code>
        {approver && a.update_waiting && <StatusBadge tone="info">New version waiting</StatusBadge>}</span>
    ) },
    { id: "open", header: "Actions", cell: (a) => <OpenAction onClick={() => open(a)} /> },
  ];
  const launchColumns: Column<Launch>[] = [
    { id: "code", header: "App", cell: (l) => <code>{l.artifact_code}</code>, sortValue: (l) => l.artifact_code },
    { id: "expires", header: "Expires", cell: (l) => <Timestamp value={l.expires_at} />, sortValue: (l) => l.expires_at },
    { id: "end", header: "Actions", cell: (l) => <EndAction onClick={() => endLaunch(l.launch_id).catch((e) => setFailure(message(e)))} /> },
  ];

  return (
    <div className="ag-stack">
      <PageHeader title="Apps" description="Apps an agent made and an admin approved. Each one opens in its own window." />
      <ViewPicker views={views.data} value={viewId} onChange={setPicked} />
      {failure && <p className="ag-field__error" role="alert">{failure}</p>}
      <DataTable caption="Apps" columns={appColumns} rows={viewId > 0 ? apps.data : []} rowKey={(a) => a.artifact_code}
        loading={views.isPending || (viewId > 0 && apps.isPending)}
        error={views.error ? message(views.error) : apps.error ? message(apps.error) : null}
        onRetry={() => void (views.error ? views.refetch() : apps.refetch())}
        emptyTitle="No apps" emptyText={views.data?.length === 0 ? "You may not launch apps in any view." : "No app is approved for this view."} />
      <SectionHeader>Open launches</SectionHeader>
      <p className="ag-muted">Opening more launches than the limit (web/launch/max_concurrent) ends the oldest one.</p>
      <DataTable caption="Open launches" columns={launchColumns} rows={launches.data?.rows} rowKey={(l) => l.launch_id}
        loading={launches.isPending} error={launches.error ? message(launches.error) : null}
        onRetry={() => void launches.refetch()} emptyTitle="No open launches" />
    </div>
  );
}
