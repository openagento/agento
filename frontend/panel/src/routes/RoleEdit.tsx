// The role page (plan "Panel roles"): Role info and Role resources, Magento ACL style. The resources
// are a checkbox tree per scope; the checked leaves are a Set held here (roleTree.ts), saved with one PUT.
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { useParams, useSearchParams } from "react-router";
import { LayoutDashboard, ShieldCheck, Wrench } from "lucide-react";
import {
  Badge, Breadcrumbs, Divider, Group, Paper, Select, Stack, Tabs, Text,
  TextInput, ThemeIcon, Tooltip, type TreeNodeData,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useErrorText, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  AppLink, Button, ConfirmDialog, ErrorState, LoadingState, PageHeader, useDocumentTitle, useFormat, useLookup, useT, type Translate,
} from "@agento/ui";
import { ResourceTree, UnsavedBar } from "./ResourceTree";
import { useScopes, type Scopes } from "./admin/ScopePicker";
import {
  changeCount, initialChecked, lockedLeaves, toPayload, toTreeData,
  type LeafInfo, type RoleResources, type TreeTexts,
} from "./roleTree";

interface RoleScope { workspace_id: number | null; agent_view_id: number | null; tools: number; operations: number }
interface RoleDetail { code: string; label: string; builtin: boolean; users: number; scopes: RoleScope[] }
type ScopeKey = string; // "global:0", "workspace:2", "agent_view:3"

/** The admin panel leaves have no workspace or view: one place, first in the picker. */
const GLOBAL: ScopeKey = "global:0";

function RoleInfo({ role }: { role: RoleDetail }) {
  const qc = useQueryClient();
  const [label, setLabel] = useState(role.label);
  const t = useT("panel");
  const errorText = useErrorText();
  const save = useMutation({
    mutationFn: () => apiFetch<RoleDetail>(`/api/admin/roles/${role.code}`, { method: "PATCH", json: { label: label.trim() } }),
    onSuccess: () => {
      notifications.show({ message: t("roles.saved") });
      void qc.invalidateQueries({ queryKey: ["role", role.code] });
      void qc.invalidateQueries({ queryKey: ["roles"] });
    },
  });
  return (
    <Paper withBorder p="lg" radius="md" maw={560}>
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <Stack gap="md">
          <TextInput label={t("roles.name")} required value={label} disabled={save.isPending} onChange={(e) => setLabel(e.currentTarget.value)}
            error={save.error ? errorText(save.error, "roles.save_failed") : null} />
          <TextInput label={t("roles.code")} value={role.code} readOnly variant="filled"
            description={t("roles.code_fixed")} />
          <Text fz="sm" c="dimmed">{t("roles.user_count", { count: role.users })}</Text>
          <Group justify="flex-end">
            <Button type="submit" variant="primary" disabled={!label.trim() || label.trim() === role.label || save.isPending}>
              {t("action.save")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Paper>
  );
}

/** The level-1 group icon, and the badges a role leaf shows after its label. */
const GROUP_ICON: Record<string, ReactNode> = {
  "grp:admin": <LayoutDashboard size={14} />, "grp:ops": <ShieldCheck size={14} />, "grp:tools": <Wrench size={14} />,
};
const roleIcon = (node: TreeNodeData): ReactNode => GROUP_ICON[node.value]
  ? <ThemeIcon variant="light" size="sm" radius="sm">{GROUP_ICON[node.value]}</ThemeIcon>
  : null;

function roleInfo(node: TreeNodeData, viaHint: string, t: Translate): ReactNode {
  const info = (node.nodeProps ?? {}) as LeafInfo;
  return (
    <>
      {info.id && <Text fz="xs" c="dimmed" ff="monospace" truncate>{info.id}</Text>}
      {info.off && (
        <Tooltip label={t("roles.off_here_hint")} withArrow multiline w={260}>
          <Badge variant="light" color="yellow" size="sm">{t("roles.off_here")}</Badge>
        </Tooltip>
      )}
      {info.via && (
        <Tooltip label={viaHint} withArrow>
          <Badge variant="light" color="blue" size="sm">{t("roles.via_workspace")}</Badge>
        </Tooltip>
      )}
      {info.builtin && (
        <Tooltip label={t("roles.builtin_leaf_hint")} withArrow>
          <Badge variant="light" color="grape" size="sm">{t("roles.builtin_leaf")}</Badge>
        </Tooltip>
      )}
    </>
  );
}

const keyOf = (s: RoleScope): ScopeKey =>
  s.agent_view_id ? `agent_view:${s.agent_view_id}` : s.workspace_id ? `workspace:${s.workspace_id}` : GLOBAL;

function RoleResourcesTab({ role }: { role: RoleDetail }) {
  const qc = useQueryClient();
  const scopes = useScopes();
  const [params, setParams] = useSearchParams();
  const [edits, setEdits] = useState<{ at: ScopeKey; set: Set<string> } | null>(null);
  const [pending, setPending] = useState<ScopeKey | null>(null);
  const t = useT("panel");
  const { number } = useFormat();
  const lookup = useLookup();
  const errorText = useErrorText();
  const texts = useMemo<TreeTexts>(() => ({
    admin: t("roles.group.admin"), operations: t("roles.group.operations"), tools: t("roles.group.tools"),
    title: (namespace, key, serverTitle) => (namespace ? lookup(namespace, key) : undefined) ?? serverTitle,
  }), [t, lookup]);
  const globalPlace = { value: GLOBAL, label: t("roles.global") };

  const counts = new Map(role.scopes.map((s) => [keyOf(s), s.tools + s.operations]));
  const options = (kind: "workspace" | "agent_view", rows: Scopes["workspaces"] = []) => rows
    .map((r) => ({ value: `${kind}:${r.id}`, label: `${r.code} — ${r.label}` }))
    .sort((a, b) => Number(counts.has(b.value)) - Number(counts.has(a.value)));
  const groups = [
    { group: t("roles.group.workspaces"), items: options("workspace", scopes.data?.workspaces) },
    { group: t("roles.group.agent_views"), items: options("agent_view", scopes.data?.agent_views) },
  ].filter((g) => g.items.length);
  const all = [globalPlace, ...groups.flatMap((g) => g.items)];
  // The URL scope when it names a known place; else the first place with access, else the Admin panel.
  const fromUrl = `${params.get("scope")}:${params.get("scope_id")}`;
  const current = all.find((o) => o.value === fromUrl)?.value ?? all.find((o) => counts.has(o.value))?.value ?? GLOBAL;
  const [kind, id] = current.split(":");
  const global = current === GLOBAL;

  const resources = useQuery({
    queryKey: ["role-resources", role.code, current], enabled: scopes.isSuccess,
    queryFn: async ({ signal }) => ({ operations: [], toolsets: [], admin: [], ...await apiFetch<Partial<RoleResources>>(
      `/api/admin/roles/${role.code}/resources?scope=${kind}${global ? "" : `&scope_id=${id}`}`, { signal }) }),
  });
  const server = useMemo(() => resources.data && initialChecked(resources.data), [resources.data]);
  const locked = useMemo(() => resources.data ? lockedLeaves(resources.data) : new Set<string>(), [resources.data]);
  const res = resources.data;
  const toData = useCallback((search: string) => (res ? toTreeData(res, search, texts) : []), [res, texts]);
  const checked = edits && edits.at === current ? edits.set : server;
  const changes = checked && server ? changeCount(checked, server) : 0;

  // The tree and the scope are locked while a save runs; on success only the edit state that was sent
  // is cleared, so nothing made meanwhile (a back-button scope change) is dropped.
  const save = useMutation({
    mutationFn: (sent: NonNullable<typeof edits>) => {
      const [k, i] = sent.at.split(":");
      return apiFetch(`/api/admin/roles/${role.code}/resources`, {
        method: "PUT", json: { scope: k, ...(k === "global" ? {} : { scope_id: Number(i) }), ...toPayload(sent.set, resources.data!) },
      });
    },
    onSuccess: async (_r, sent) => {
      notifications.show({ message: t("roles.access_saved") });
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["role-resources", role.code] }),
        qc.invalidateQueries({ queryKey: ["role", role.code] }),
        qc.invalidateQueries({ queryKey: ["roles"] }),
      ]);
      setEdits((e) => (e === sent ? null : e));
    },
    onError: (e) => notifications.show({ color: "red", message: errorText(e, "roles.access_save_failed") }),
  });

  const go = (to: ScopeKey) => {
    const [k, i] = to.split(":");
    setEdits(null);
    setPending(null);
    setParams((p) => { const next = new URLSearchParams(p); next.set("scope", k); next.set("scope_id", i); return next; });
  };
  const pick = (to: string | null) => {
    if (!to || to === current) return;
    if (changes) setPending(to); else go(to);
  };
  const ws = kind === "agent_view" ? scopes.data?.workspaces.find((w) =>
    w.id === scopes.data?.agent_views.find((v) => v.id === Number(id))?.workspace_id) : undefined;
  const viaHint = ws ? t("roles.via_workspace_hint", { workspace: `${ws.code} — ${ws.label}` }) : t("roles.via_own_workspace_hint");
  const renderInfo = useCallback((node: TreeNodeData) => roleInfo(node, viaHint, t), [viaHint, t]);

  if (scopes.isPending) return <LoadingState />;
  if (scopes.error) return <ErrorState message={errorText(scopes.error, "roles.scopes_load_failed")} onRetry={() => void scopes.refetch()} />;

  return (
    <Stack gap="md">
      <Paper withBorder radius="md">
        <Group p="md" justify="space-between" wrap="wrap" gap="sm">
          <Stack gap={0}>
            <Text fw={600}>{t("roles.where")}</Text>
            <Text fz="sm" c="dimmed">{t("roles.where_hint")}</Text>
          </Stack>
          <Select aria-label={t("scope.label")} data={[globalPlace, ...groups]} value={current} onChange={pick} allowDeselect={false} searchable miw={280}
            disabled={save.isPending}
            renderOption={({ option }) => (
              <Group flex={1} justify="space-between" wrap="nowrap" gap="xs">
                <Text fz="sm">{option.label}</Text>
                {counts.get(option.value) ? (
                  <Badge size="xs" variant="light" circle>{number(counts.get(option.value) ?? 0)}</Badge>
                ) : null}
              </Group>
            )} />
        </Group>
        <Divider />
        <Stack p="md" gap="sm">
          {resources.isPending ? <LoadingState /> : resources.error
            ? <ErrorState message={errorText(resources.error, "roles.resources_load_failed")} onRetry={() => void resources.refetch()} />
            : <ResourceTree key={current} toData={toData} checked={checked!} locked={locked}
              onChange={(set) => setEdits({ at: current, set })} busy={save.isPending}
              renderIcon={roleIcon} renderInfo={renderInfo}
              emptyTitle={t("roles.nothing.title")} emptyText={t("roles.nothing.text")} />}
        </Stack>
      </Paper>
      {changes > 0 && (
        <UnsavedBar changes={changes} busy={save.isPending} onReset={() => setEdits(null)}
          onSave={() => edits && save.mutate(edits)} />
      )}
      <ConfirmDialog opened={pending !== null} title={t("roles.discard.title")} danger confirmLabel={t("roles.discard.confirm")}
        onCancel={() => setPending(null)} onConfirm={() => pending && go(pending)}>
        <p>{t("roles.discard.text", { count: changes })}</p>
      </ConfirmDialog>
    </Stack>
  );
}

export function RoleEdit() {
  const { code = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "resources" ? "resources" : "info";
  const role = useQuery({
    queryKey: ["role", code], queryFn: ({ signal }) => apiFetch<RoleDetail>(`/api/admin/roles/${code}`, { signal }),
  });
  const t = useT("panel");
  const errorText = useErrorText();
  useDocumentTitle(role.data ? t("roles.document_title", { label: role.data.label }) : t("roles.title"));
  const setTab = (picked: string | null) => setParams((p) => {
    const next = new URLSearchParams(p);
    next.set("tab", picked === "resources" ? "resources" : "info");
    return next;
  });
  return (
    <Stack gap="md">
      <Breadcrumbs>
        <AppLink to="/users/roles" fz="sm">{t("roles.title")}</AppLink>
        <Text fz="sm" c="dimmed">{role.data?.label ?? code}</Text>
      </Breadcrumbs>
      {role.isPending ? <LoadingState /> : role.error
        ? <ErrorState message={errorText(role.error, "roles.one_load_failed")} onRetry={() => void role.refetch()} />
        : (
          <>
            <PageHeader title={<Group gap="sm" component="span">{role.data.label}
              {role.data.builtin && <Badge variant="light" color="gray">{t("roles.builtin")}</Badge>}</Group>}
              description={<Text component="span" ff="monospace" fz="sm">{role.data.code}</Text>} />
            <Tabs variant="outline" value={tab} onChange={setTab}>
              <Tabs.List>
                <Tabs.Tab value="info">{t("roles.tab.info")}</Tabs.Tab>
                <Tabs.Tab value="resources">{t("roles.tab.resources")}</Tabs.Tab>
              </Tabs.List>
              <Tabs.Panel value="info" pt="md"><RoleInfo role={role.data} /></Tabs.Panel>
              <Tabs.Panel value="resources" pt="md"><RoleResourcesTab role={role.data} /></Tabs.Panel>
            </Tabs>
          </>
        )}
    </Stack>
  );
}
