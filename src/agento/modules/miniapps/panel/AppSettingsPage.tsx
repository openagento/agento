// An app's settings (D-MINIAPP-3): title and icon in the header, members, the approval per view. The API
// checks the per-app rights (`edit`, `remove`) and `artifact.launch` in the view; this page is only the form.
import { useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { apiFetch, openLaunch, useCan, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  AppIcon, AppLink, Button, ConfirmDialog, DataTable, DeleteAction, EditableTitle, ErrorState, IconPicker, LoadingState,
  OpenAction, PageHeader, SectionHeader, SelectField, Timestamp, type Column,
} from "@agento/ui";
import { message, useArtifactsAvailable, useViews, ViewPicker } from "./common";

interface Member { id: number; user_id: number | null; username: string | null; role: string | null; can_edit: boolean; can_remove: boolean }
interface AppData {
  artifact_code: string; default_title: string | null; title: string | null; icon: string | null;
  can: { view: boolean; edit: boolean; remove: boolean }; members: Member[] | null;
}
interface Approval { approved_version_id: string; allowed_actions: string[]; activated_by: string; activated_at: string }
interface Page { approval: Approval | null; current: { version_id: string; title: string | null; actions: string[] | null } }
interface Toolset { toolset: string; tools: { name: string; enabled: boolean }[] }
interface Options { users: string[]; roles: { code: string; label: string }[] }

const appUrl = (code: string) => `/api/miniapps/apps/${encodeURIComponent(code)}`;

export default function AppSettingsPage() {
  const code = useParams().code ?? "";
  const app = useQuery({
    queryKey: ["miniapps", "app", code],
    queryFn: ({ signal }) => apiFetch<AppData>(appUrl(code), { signal }),
  });
  const a = app.data;
  const title = a ? a.title ?? a.default_title ?? code : code;
  const refresh = useRefresh();
  const artifacts = useArtifactsAvailable();
  // Saved in the background; an empty title goes back to the agent's.
  const patch = useMutation({
    mutationFn: (changes: { title?: string; icon?: string | null }) => apiFetch(appUrl(code), { method: "PATCH", json: changes }),
    onSettled: () => void refresh(),
  });
  return (
    <div className="ag-stack">
      <PageHeader
        title={a?.can.edit
          ? <span className="ag-row"><IconPicker value={a.icon} onChange={(icon) => patch.mutate({ icon })} />
            <EditableTitle value={title} maxLength={255} onSave={(t) => patch.mutate({ title: t })} /></span>
          : <span className="ag-row">{a && <AppIcon name={a.icon} size={24} />}{title}</span>}
        description={<>App settings. <code>{code}</code>
          {artifacts && <> · <AppLink to={`/artifacts/${code}`}>Versions and history</AppLink></>}</>} />
      {patch.error && <p className="ag-field__error" role="alert">{message(patch.error)}</p>}
      {app.error ? <ErrorState message={message(app.error)} onRetry={() => void app.refetch()} />
        : !a ? <LoadingState />
          : !a.can.edit ? <p className="ag-muted">You may open this app, but not change it.</p>
            : (
              <>
                <Members app={a} />
                <Approvals code={code} />
              </>
            )}
    </div>
  );
}

function useRefresh() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["miniapps"] });
}

function Members({ app }: { app: AppData }) {
  const refresh = useRefresh();
  const url = `${appUrl(app.artifact_code)}/members`;
  const [pick, setPick] = useState("");
  const options = useQuery({
    queryKey: ["miniapps", "member-options", app.artifact_code],
    queryFn: ({ signal }) => apiFetch<Options>(`${appUrl(app.artifact_code)}/member-options`, { signal }),
  });
  const [failure, setFailure] = useState<string | null>(null);
  const done = { onSuccess: () => setFailure(null), onError: (e: unknown) => setFailure(message(e)), onSettled: () => void refresh() };
  const put = useMutation({
    mutationFn: (m: { username?: string; role?: string; can_edit: boolean; can_remove: boolean }) => apiFetch(url, { method: "PUT", json: m }),
    ...done,
  });
  const drop = useMutation({ mutationFn: (id: number) => apiFetch(`${url}/${id}`, { method: "DELETE" }), ...done });
  const who = (m: Member) => (m.user_id !== null ? { username: m.username ?? "" } : { role: m.role ?? "" });
  const flag = (m: Member, key: "can_edit" | "can_remove", label: string) => (
    <input type="checkbox" aria-label={`${label}: ${m.username ?? m.role}`} checked={m[key]}
      disabled={put.isPending || (key === "can_remove" && !app.can.remove)}
      onChange={() => put.mutate({ ...who(m), can_edit: m.can_edit, can_remove: m.can_remove, [key]: !m[key] })} />
  );
  const columns: Column<Member>[] = [
    { id: "who", header: "Member", cell: (m) => (m.user_id !== null ? m.username : <>Role <code>{m.role}</code></>) },
    { id: "edit", header: "Edit", cell: (m) => flag(m, "can_edit", "Edit") },
    { id: "remove", header: "Remove", cell: (m) => flag(m, "can_remove", "Remove") },
    { id: "actions", header: "Actions", cell: (m) => <DeleteAction label="Remove member" onClick={() => drop.mutate(m.id)} /> },
  ];
  return (
    <section className="ag-stack">
      <SectionHeader>Members</SectionHeader>
      <p className="ag-muted">A member sees the app. To run it, the member's role also needs artifact.launch and a tool grant for each of its actions in the view.</p>
      {failure && <p className="ag-field__error" role="alert">{failure}</p>}
      <DataTable caption="Members" columns={columns} rows={app.members ?? []} rowKey={(m) => String(m.id)}
        emptyTitle="No members" emptyText="Only the creator and admins see this app." />
      <form className="ag-row" onSubmit={(e) => {
        e.preventDefault();
        // `username:<name>` or `role:<code>`: the field the API takes, then its value.
        const [key, ...rest] = pick.split(":");
        put.mutate({ [key]: rest.join(":"), can_edit: false, can_remove: false });
        setPick("");
      }}>
        <SelectField label="Add a user or a role" name="member" searchable placeholder="Search" value={pick} onChange={setPick}
          options={memberChoices(options.data, app.members ?? [])} />
        <Button type="submit" disabled={put.isPending || !pick}>Add member</Button>
      </form>
    </section>
  );
}

/** The users and roles not yet members, grouped, as `username:<name>` / `role:<code>`. */
function memberChoices(o: Options | undefined, members: Member[]) {
  if (!o) return [];
  const users = new Set(members.map((m) => m.username));
  const roles = new Set(members.map((m) => m.role));
  return [
    { group: "Users", items: o.users.filter((u) => !users.has(u)).map((u) => ({ value: `username:${u}`, label: u })) },
    { group: "Roles", items: o.roles.filter((r) => !roles.has(r.code)).map((r) => ({ value: `role:${r.code}`, label: r.label })) },
  ];
}

function Approvals({ code }: { code: string }) {
  const views = useViews();
  const [picked, setPicked] = useState("");
  // `?view=<id>` (from the artifact page) picks the view, if it is one of the user's.
  const linked = Number(useSearchParams()[0].get("view"));
  const viewId = Number(picked || (views.data?.find((v) => v.id === linked) ?? views.data?.[0])?.id || 0);
  const url = `${appUrl(code)}/views/${viewId}/approval`;
  const page = useQuery({
    queryKey: ["miniapps", "approval", code, viewId],
    enabled: viewId > 0,
    queryFn: ({ signal }) => apiFetch<Page>(url, { signal }),
  });
  const p = page.data;
  return (
    <section className="ag-stack">
      <SectionHeader>Approval</SectionHeader>
      <p className="ag-muted">The app runs its actions with the tools and accounts of one agent view. Approve a version in each view where it may run.</p>
      <ViewPicker views={views.data} value={viewId} onChange={setPicked} />
      {views.error || page.error ? <ErrorState message={message(views.error ?? page.error)} onRetry={() => void page.refetch()} />
        : !p ? (views.data?.length === 0 ? <p className="ag-muted">You may not approve apps in any view.</p> : <LoadingState />)
          : <Approve key={`${viewId}:${p.current.version_id}:${p.approval?.approved_version_id}`} code={code} viewId={viewId} url={url} page={p} />}
    </section>
  );
}

function Approve({ code, viewId, url, page }: { code: string; viewId: number; url: string; page: Page }) {
  const refresh = useRefresh();
  const { approval, current } = page;
  const actions = current.actions ?? [];
  const [checked, setChecked] = useState(() => new Set(approval ? approval.allowed_actions : actions));
  const [failure, setFailure] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);
  const done = { onSuccess: () => setFailure(null), onError: (e: unknown) => setFailure(message(e)), onSettled: () => { setRevoking(false); void refresh(); } };
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
        {approval && <DeleteAction label="Revoke" onClick={() => setRevoking(true)} disabled={revoke.isPending} />}
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
      <ConfirmDialog opened={revoking} title="Revoke approval" confirmLabel="Revoke" danger busy={revoke.isPending}
        onCancel={() => setRevoking(false)} onConfirm={() => revoke.mutate()}>
        <p>Are you sure you want to revoke the approval in this view? Its open launches stop working.</p>
      </ConfirmDialog>
    </>
  );
}
