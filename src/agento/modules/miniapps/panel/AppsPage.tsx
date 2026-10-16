// The apps catalogue (D-MINIAPP-3): every app the user may view, as tiles or a list, and the open
// launches. The API decides the rights (`can`); this page only shows them.
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { apiFetch, LIVE_REFETCH_MS, endLaunch, openLaunch, reconcileLaunches, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  AppIcon, AppLink, AppTile, ConfirmDialog, DataTable, DeleteAction, EmptyState, EndAction, ErrorState, LoadingState,
  OpenMenuAction, PageHeader, SectionHeader, SettingsAction, StatusBadge, TileGrid, Timestamp, ViewToggle, type Column,
  type TileMode,
} from "@agento/ui";
import { message, useArtifactsAvailable } from "./common";

interface AppView { agent_view_id: number; label: string; version_id: string; execute: boolean; update_waiting?: boolean; approve_again?: boolean }
export interface App {
  artifact_code: string; title: string; icon: string | null;
  can: { view: boolean; edit: boolean; remove: boolean; execute: boolean }; views: AppView[];
}
interface Launch { launch_id: string; artifact_code: string; version_id: string; agent_view_id: number; expires_at: string }

const MODE_KEY = "agento.apps.view";
const readMode = (): TileMode => {
  try { return localStorage.getItem(MODE_KEY) === "list" ? "list" : "tiles"; } catch { return "tiles"; }
};
const writeMode = (m: TileMode) => {
  try { localStorage.setItem(MODE_KEY, m); } catch { /* storage blocked: the choice lasts this visit */ }
};

const choicesOf = (a: App) => a.views.filter((v) => v.execute).map((v) => ({ value: String(v.agent_view_id), label: v.label }));

function Badge({ app }: { app: App }) {
  if (!app.can.edit) return null;
  if (app.views.some((v) => v.approve_again)) return <StatusBadge tone="info">Approve again</StatusBadge>;
  if (app.views.some((v) => v.update_waiting)) return <StatusBadge tone="info">New version waiting</StatusBadge>;
  return null;
}

export default function AppsPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [mode, setMode] = useState<TileMode>(readMode);
  const artifacts = useArtifactsAvailable();
  const apps = useQuery({
    queryKey: ["miniapps", "apps"], refetchInterval: LIVE_REFETCH_MS,
    queryFn: ({ signal }) => apiFetch<App[]>("/api/miniapps/apps", { signal }),
  });
  const launches = useQuery({
    queryKey: ["launches"],
    queryFn: async ({ signal }) => {
      const requestedAt = Date.now();
      return { requestedAt, rows: await apiFetch<Launch[]>("/api/launches", { signal }) };
    },
  });
  const [failure, setFailure] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<App | null>(null);
  const remove = useMutation({
    mutationFn: (a: App) => apiFetch(`/api/miniapps/apps/${encodeURIComponent(a.artifact_code)}`, { method: "DELETE" }),
    onSuccess: () => setDeleting(null),
    onError: (e: unknown) => { setDeleting(null); setFailure(message(e)); },
    onSettled: () => void qc.invalidateQueries({ queryKey: ["miniapps"] }),
  });
  const live = new Set((launches.data?.rows ?? []).map((l) => l.launch_id));
  const listed = launches.data;
  useEffect(() => {
    if (listed) reconcileLaunches(new Set(listed.rows.map((l) => l.launch_id)), listed.requestedAt);
  }, [listed]);

  const open = (a: App, view?: string) => {
    setFailure(null);
    // No await before openLaunch: it reserves the window inside this click.
    openLaunch({ agentViewId: Number(view), artifactCode: a.artifact_code }, live)
      .then((r) => { if (r.kind === "blocked") setFailure("The browser blocked the window. Allow pop-ups for the panel and try again."); })
      .catch((e) => setFailure(message(e)));
  };
  const toMode = (m: TileMode) => { setMode(m); writeMode(m); };
  const actions = (a: App) => (
    <>
      {a.can.edit && <SettingsAction onClick={() => void navigate(`/apps/${encodeURIComponent(a.artifact_code)}`)} />}
      {a.can.remove && <DeleteAction onClick={() => setDeleting(a)} />}
    </>
  );

  const columns: Column<App>[] = [
    { id: "title", header: "App", sortValue: (a) => a.title,
      cell: (a) => <span className="ag-row"><AppIcon name={a.icon} />{a.title}<Badge app={a} /></span> },
    { id: "views", header: "Agent views", cell: (a) => a.views.map((v) => v.label).join(", ") },
    ...(artifacts ? [{ id: "artifact", header: "Artifact", cell: (a: App) => (
      <AppLink to={`/artifacts/${a.artifact_code}`}>Versions</AppLink>) }] : []),
    { id: "actions", header: "Actions", cell: (a) => (
      <span className="ag-row"><OpenMenuAction items={choicesOf(a)} onSelect={(v) => open(a, v)} />{actions(a)}</span>
    ) },
  ];
  const launchColumns: Column<Launch>[] = [
    { id: "code", header: "App", cell: (l) => <code>{l.artifact_code}</code>, sortValue: (l) => l.artifact_code },
    { id: "expires", header: "Expires", cell: (l) => <Timestamp value={l.expires_at} />, sortValue: (l) => l.expires_at },
    { id: "end", header: "Actions", cell: (l) => <EndAction onClick={() => endLaunch(l.launch_id).catch((e) => setFailure(message(e)))} /> },
  ];
  const empty = { title: "No apps", text: "No app is shared with you yet." };

  return (
    <div className="ag-stack">
      <PageHeader title="Apps" description="Apps an agent made. Each one opens in its own window."
        actions={<ViewToggle value={mode} onChange={toMode} />} />
      {failure && <p className="ag-field__error" role="alert">{failure}</p>}
      {mode === "list" ? (
        <DataTable caption="Apps" columns={columns} rows={apps.data} rowKey={(a) => a.artifact_code}
          loading={apps.isPending} error={apps.error ? message(apps.error) : null} onRetry={() => void apps.refetch()}
          emptyTitle={empty.title} emptyText={empty.text} />
      ) : apps.error ? <ErrorState message={message(apps.error)} onRetry={() => void apps.refetch()} />
        : !apps.data ? <LoadingState />
          : apps.data.length === 0 ? <EmptyState title={empty.title}>{empty.text}</EmptyState>
            : (
              <TileGrid label="Apps">
                {apps.data.map((a) => (
                  <AppTile key={a.artifact_code} icon={a.icon} title={a.title} disabled={!a.can.execute}
                    choices={choicesOf(a)} onOpen={(v) => open(a, v)} badge={<Badge app={a} />} actions={actions(a)} />
                ))}
              </TileGrid>
            )}
      <SectionHeader>Open launches</SectionHeader>
      <p className="ag-muted">Opening more launches than the limit (web/launch/max_concurrent) ends the oldest one.</p>
      <DataTable caption="Open launches" columns={launchColumns} rows={launches.data?.rows} rowKey={(l) => l.launch_id}
        loading={launches.isPending} error={launches.error ? message(launches.error) : null}
        onRetry={() => void launches.refetch()} emptyTitle="No open launches" />
      <ConfirmDialog opened={deleting !== null} title="Delete app" confirmLabel="Delete" danger busy={remove.isPending}
        onCancel={() => setDeleting(null)} onConfirm={() => deleting && remove.mutate(deleting)}>
        <p>Are you sure you want to delete {deleting?.title}? This deletes the app and all its versions.</p>
      </ConfirmDialog>
    </div>
  );
}
