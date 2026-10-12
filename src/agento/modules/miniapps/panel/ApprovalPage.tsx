// The approval page (D-MINIAPP-2): approve `current` for one view with a subset of its manifest
// actions, revoke, or preview `current` with no actions. The API checks the leaf
// `miniapps.approve` and `artifact.launch` in the view; this page is only the form.
import { useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { apiFetch, openLaunch, useCan, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, DataTable, DeleteAction, ErrorState, LoadingState, OpenAction, PageHeader, SectionHeader, Timestamp, type Column } from "@agento/ui";
import { message, useViews, ViewPicker } from "./common";

interface Approval { approved_version_id: string; allowed_actions: string[]; activated_by: string; activated_at: string }
interface Page { approval: Approval | null; current: { version_id: string; title: string | null; actions: string[] | null } }
interface Toolset { toolset: string; tools: { name: string; enabled: boolean }[] }

export default function ApprovalPage() {
  const code = useParams().code ?? "";
  const views = useViews();
  const [picked, setPicked] = useState("");
  // `?view=<id>` (from the catalogue or the artifact page) picks the view, if it is one of the user's.
  const linked = Number(useSearchParams()[0].get("view"));
  const viewId = Number(picked || (views.data?.find((v) => v.id === linked) ?? views.data?.[0])?.id || 0);
  const url = `/api/miniapps/apps/${encodeURIComponent(code)}/views/${viewId}/approval`;
  const page = useQuery({
    queryKey: ["miniapps", "approval", code, viewId],
    enabled: viewId > 0,
    queryFn: ({ signal }) => apiFetch<Page>(url, { signal }),
  });
  const p = page.data;
  return (
    <div className="ag-stack">
      <PageHeader title={p?.current.title ?? code} description={<>Approve an app for a view. <code>{code}</code></>} />
      <ViewPicker views={views.data} value={viewId} onChange={setPicked} />
      {views.error || page.error ? <ErrorState message={message(views.error ?? page.error)} onRetry={() => void page.refetch()} />
        : !p ? (views.data?.length === 0 ? <ErrorState message="You may not launch apps in any view." /> : <LoadingState />)
          : <Approve key={`${viewId}:${p.current.version_id}:${p.approval?.approved_version_id}`} code={code} viewId={viewId} url={url} page={p} />}
    </div>
  );
}

function Approve({ code, viewId, url, page }: { code: string; viewId: number; url: string; page: Page }) {
  const qc = useQueryClient();
  const { approval, current } = page;
  const actions = current.actions ?? [];
  const [checked, setChecked] = useState(() => new Set(approval ? approval.allowed_actions : actions));
  const [failure, setFailure] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["miniapps"] });
  const done = { onSuccess: () => { setFailure(null); void refresh(); }, onError: (e: unknown) => { setFailure(message(e)); void refresh(); } };
  const approve = useMutation({
    mutationFn: () => apiFetch(url, { method: "POST", json: { expected_version_id: current.version_id, actions: actions.filter((a) => checked.has(a)) } }),
    ...done,
  });
  const revoke = useMutation({ mutationFn: () => apiFetch(url, { method: "DELETE" }), ...done });
  const canTools = useCan("admin.tools");
  const tools = useQuery({
    queryKey: ["admin", "tools", viewId],
    enabled: canTools,
    queryFn: ({ signal }) => apiFetch<Toolset[]>(`/api/admin/tools?scope=agent_view&scope_id=${viewId}`, { signal }),
  });
  const enabled = new Map((tools.data ?? []).flatMap((t) => t.tools.map((x) => [x.name, x.enabled] as const)));
  const preview = () => {
    setFailure(null);
    // No await before openLaunch: it reserves the window inside this click.
    openLaunch({ agentViewId: viewId, artifactCode: code, preview: true }, new Set())
      .then((r) => { if (r.kind === "blocked") setFailure("The browser blocked the window. Allow pop-ups for the panel and try again."); })
      .catch((e) => setFailure(message(e)));
  };
  const toggle = (a: string) => setChecked((s) => {
    const n = new Set(s);
    if (!n.delete(a)) n.add(a);
    return n;
  });

  const columns: Column<{ name: string }>[] = [
    { id: "action", header: "Action", cell: ({ name }) => (
      <label className="ag-row"><input type="checkbox" checked={checked.has(name)} onChange={() => toggle(name)} /><code>{name}</code></label>
    ) },
    { id: "approved", header: "Approved now", cell: ({ name }) => (approval?.allowed_actions.includes(name) ? "yes" : "no") },
    ...(tools.data ? [{ id: "tool", header: "Tool in view", cell: ({ name }: { name: string }) => (enabled.get(name) ? "on" : "off") }] : []),
  ];

  return (
    <>
      <div className="ag-row">
        <OpenAction label="Preview current" onClick={preview} />
        {approval && <DeleteAction label="Revoke" onClick={() => revoke.mutate()} disabled={revoke.isPending} />}
      </div>
      {failure && <p className="ag-field__error" role="alert">{failure}</p>}
      <p>
        {approval
          ? <>Approved <code>{approval.approved_version_id}</code> by {approval.activated_by}, <Timestamp value={approval.activated_at} />.</>
          : "Not approved for this view."}
        {" "}Current <code>{current.version_id}</code>.
        {approval && approval.approved_version_id !== current.version_id && " A new version is waiting."}
      </p>
      <SectionHeader>Actions in the current version</SectionHeader>
      {current.actions === null ? <ErrorState title="The manifest is not valid" message="The current version cannot be approved." /> : (
        <form className="ag-stack" onSubmit={(e) => { e.preventDefault(); approve.mutate(); }}>
          <DataTable caption="Actions" columns={columns} rows={actions.map((name) => ({ name }))} rowKey={(a) => a.name}
            emptyTitle="No actions" emptyText="The app opens with its files only." />
          <p className="ag-muted">An action works only while its tool is on for this view (Tools).</p>
          <div className="ag-row"><Button type="submit" variant="primary" disabled={approve.isPending}>Approve current</Button></div>
        </form>
      )}
    </>
  );
}
