// An app's settings (D-MINIAPP-3): title and icon in the header, members, the approval per view. The API
// checks the per-app rights (`edit`, `remove`) and `artifact.launch` in the view; this page is only the form.
import { useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { apiFetch, openLaunch, useCan, useErrorText, useMutation, useQuery, useQueryClient, type Failure } from "@agento/api";
import {
  AppIcon, AppLink, Button, ConfirmDialog, DataTable, DeleteAction, EditableTitle, ErrorState, IconPicker, LoadingState,
  OpenAction, PageHeader, SectionHeader, SelectField, Timestamp, useDocumentTitle, useT, withElements, type Column, type Translate,
} from "@agento/ui";
import { useArtifactsAvailable, useViews, ViewPicker } from "./common";

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
  const t = useT("miniapps");
  const errorText = useErrorText("miniapps");
  const code = useParams().code ?? "";
  const app = useQuery({
    queryKey: ["miniapps", "app", code],
    queryFn: ({ signal }) => apiFetch<AppData>(appUrl(code), { signal }),
  });
  const a = app.data;
  const title = a ? a.title ?? a.default_title ?? code : code;
  useDocumentTitle(title);
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
            <EditableTitle value={title} maxLength={255} onSave={(next) => patch.mutate({ title: next })} /></span>
          : <span className="ag-row">{a && <AppIcon name={a.icon} size={24} />}{title}</span>}
        description={<>{t("settings.description")} <code>{code}</code>
          {artifacts && <> · <AppLink to={`/artifacts/${code}`}>{t("settings.versions_link")}</AppLink></>}</>} />
      {patch.error && <p className="ag-field__error" role="alert">{errorText(patch.error, "settings.save_failed")}</p>}
      {app.error ? <ErrorState message={errorText(app.error, "settings.load_failed")} onRetry={() => void app.refetch()} />
        : !a ? <LoadingState />
          : !a.can.edit ? <p className="ag-muted">{t("settings.view_only")}</p>
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
  const t = useT("miniapps");
  const errorText = useErrorText("miniapps");
  const refresh = useRefresh();
  const url = `${appUrl(app.artifact_code)}/members`;
  const [pick, setPick] = useState("");
  const options = useQuery({
    queryKey: ["miniapps", "member-options", app.artifact_code],
    queryFn: ({ signal }) => apiFetch<Options>(`${appUrl(app.artifact_code)}/member-options`, { signal }),
  });
  const [failure, setFailure] = useState<Failure | null>(null);
  const done = { onSuccess: () => setFailure(null), onError: (e: unknown) => setFailure({ error: e, fallbackKey: "settings.members.save_failed" }), onSettled: () => void refresh() };
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
    { id: "who", header: t("settings.members.column.member"),
      cell: (m) => (m.user_id !== null ? m.username : withElements(t("settings.members.role"), { role: <code>{m.role}</code> })) },
    { id: "edit", header: t("settings.members.column.edit"), cell: (m) => flag(m, "can_edit", t("settings.members.column.edit")) },
    { id: "remove", header: t("settings.members.column.remove"), cell: (m) => flag(m, "can_remove", t("settings.members.column.remove")) },
    { id: "actions", header: t("settings.members.column.actions"),
      cell: (m) => <DeleteAction label={t("settings.members.remove_member")} onClick={() => drop.mutate(m.id)} /> },
  ];
  return (
    <section className="ag-stack">
      <SectionHeader>{t("settings.members.title")}</SectionHeader>
      <p className="ag-muted">{t("settings.members.hint")}</p>
      {failure && <p className="ag-field__error" role="alert">{errorText(failure.error, failure.fallbackKey)}</p>}
      <DataTable caption={t("settings.members.title")} columns={columns} rows={app.members ?? []} rowKey={(m) => String(m.id)}
        emptyTitle={t("settings.members.empty.title")} emptyText={t("settings.members.empty.text")} />
      <form className="ag-row" onSubmit={(e) => {
        e.preventDefault();
        // `username:<name>` or `role:<code>`: the field the API takes, then its value.
        const [key, ...rest] = pick.split(":");
        put.mutate({ [key]: rest.join(":"), can_edit: false, can_remove: false });
        setPick("");
      }}>
        <SelectField label={t("settings.members.add.label")} name="member" searchable placeholder={t("settings.members.add.placeholder")}
          value={pick} onChange={setPick} options={memberChoices(t, options.data, app.members ?? [])} />
        <Button type="submit" disabled={put.isPending || !pick}>{t("settings.members.add.submit")}</Button>
      </form>
    </section>
  );
}

/** The users and roles not yet members, grouped, as `username:<name>` / `role:<code>`. */
function memberChoices(t: Translate, o: Options | undefined, members: Member[]) {
  if (!o) return [];
  const users = new Set(members.map((m) => m.username));
  const roles = new Set(members.map((m) => m.role));
  return [
    { group: t("settings.members.group.users"), items: o.users.filter((u) => !users.has(u)).map((u) => ({ value: `username:${u}`, label: u })) },
    { group: t("settings.members.group.roles"), items: o.roles.filter((r) => !roles.has(r.code)).map((r) => ({ value: `role:${r.code}`, label: r.label })) },
  ];
}

function Approvals({ code }: { code: string }) {
  const t = useT("miniapps");
  const errorText = useErrorText("miniapps");
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
      <SectionHeader>{t("settings.approval.title")}</SectionHeader>
      <p className="ag-muted">{t("settings.approval.hint")}</p>
      <ViewPicker views={views.data} value={viewId} onChange={setPicked} />
      {views.error || page.error ? <ErrorState message={errorText(views.error ?? page.error, "settings.approval.load_failed")} onRetry={() => void page.refetch()} />
        : !p ? (views.data?.length === 0 ? <p className="ag-muted">{t("settings.approval.no_views")}</p> : <LoadingState />)
          : <Approve key={`${viewId}:${p.current.version_id}:${p.approval?.approved_version_id}`} code={code} viewId={viewId} url={url} page={p} />}
    </section>
  );
}

function Approve({ code, viewId, url, page }: { code: string; viewId: number; url: string; page: Page }) {
  const t = useT("miniapps");
  const errorText = useErrorText("miniapps");
  const refresh = useRefresh();
  const { approval, current } = page;
  const actions = current.actions ?? [];
  const [checked, setChecked] = useState(() => new Set(approval ? approval.allowed_actions : actions));
  const [failure, setFailure] = useState<Failure | null>(null);
  const [revoking, setRevoking] = useState(false);
  const done = (failed: Failure) => ({
    onSuccess: () => setFailure(null), onError: (e: unknown) => setFailure({ ...failed, error: e }),
    onSettled: () => { setRevoking(false); void refresh(); },
  });
  const approve = useMutation({
    mutationFn: () => apiFetch(url, { method: "POST", json: { expected_version_id: current.version_id, actions: actions.filter((a) => checked.has(a)) } }),
    ...done({ fallbackKey: "settings.approval.approve_failed" }),
  });
  const revoke = useMutation({ mutationFn: () => apiFetch(url, { method: "DELETE" }), ...done({ fallbackKey: "settings.approval.revoke_failed" }) });
  const canTools = useCan("admin.tools");
  const tools = useQuery({
    queryKey: ["admin", "tools", viewId],
    enabled: canTools,
    queryFn: ({ signal }) => apiFetch<Toolset[]>(`/api/admin/tools?scope=agent_view&scope_id=${viewId}`, { signal }),
  });
  const enabled = new Map((tools.data ?? []).flatMap((set) => set.tools.map((x) => [x.name, x.enabled] as const)));
  const preview = () => {
    setFailure(null);
    // No await before openLaunch: it reserves the window inside this click.
    openLaunch({ agentViewId: viewId, artifactCode: code, preview: true }, new Set())
      .then((r) => { if (r.kind === "blocked") setFailure({ fallbackKey: "launch.blocked" }); })
      .catch((e: unknown) => setFailure({ error: e, fallbackKey: "launch.failed" }));
  };
  const toggle = (a: string) => setChecked((s) => {
    const n = new Set(s);
    if (!n.delete(a)) n.add(a);
    return n;
  });

  const columns: Column<{ name: string }>[] = [
    { id: "action", header: t("settings.actions.column.action"), cell: ({ name }) => (
      <label className="ag-row"><input type="checkbox" checked={checked.has(name)} onChange={() => toggle(name)} /><code>{name}</code></label>
    ) },
    { id: "approved", header: t("settings.actions.column.approved"),
      cell: ({ name }) => (approval?.allowed_actions.includes(name) ? t("settings.actions.yes") : t("settings.actions.no")) },
    ...(tools.data ? [{ id: "tool", header: t("settings.actions.column.tool"),
      cell: ({ name }: { name: string }) => (enabled.get(name) ? t("settings.actions.on") : t("settings.actions.off")) }] : []),
  ];

  return (
    <>
      <div className="ag-row">
        <OpenAction label={t("settings.approval.preview")} onClick={preview} />
        {approval && <DeleteAction label={t("settings.approval.revoke")} onClick={() => setRevoking(true)} disabled={revoke.isPending} />}
      </div>
      {failure && <p className="ag-field__error" role="alert">{errorText(failure.error, failure.fallbackKey)}</p>}
      <p>
        {approval
          ? withElements(t("settings.approval.approved"), {
            version: <code>{approval.approved_version_id}</code>, actor: approval.activated_by,
            time: <Timestamp value={approval.activated_at} />,
          })
          : t("settings.approval.not_approved")}
        {" "}{withElements(t("settings.approval.current"), { version: <code>{current.version_id}</code> })}
        {approval && approval.approved_version_id !== current.version_id && ` ${t("settings.approval.waiting")}`}
      </p>
      <SectionHeader>{t("settings.actions.title")}</SectionHeader>
      {current.actions === null ? <ErrorState title={t("settings.actions.manifest_invalid.title")} message={t("settings.actions.manifest_invalid.text")} /> : (
        <form className="ag-stack" onSubmit={(e) => { e.preventDefault(); approve.mutate(); }}>
          <DataTable caption={t("settings.actions.caption")} columns={columns} rows={actions.map((name) => ({ name }))} rowKey={(a) => a.name}
            emptyTitle={t("settings.actions.empty.title")} emptyText={t("settings.actions.empty.text")} />
          <p className="ag-muted">{t("settings.actions.hint")}</p>
          <div className="ag-row"><Button type="submit" variant="primary" disabled={approve.isPending}>{t("settings.actions.approve")}</Button></div>
        </form>
      )}
      <ConfirmDialog opened={revoking} title={t("settings.revoke.title")} confirmLabel={t("settings.revoke.confirm")} danger busy={revoke.isPending}
        onCancel={() => setRevoking(false)} onConfirm={() => revoke.mutate()}>
        <p>{t("settings.revoke.question")}</p>
      </ConfirmDialog>
    </>
  );
}
