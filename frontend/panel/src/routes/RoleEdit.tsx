// The role page (plan "Panel roles"): Role info and Role resources, Magento ACL style. The resources
// are a checkbox tree per scope; the checked leaves are a Set held here (roleTree.ts), saved with one PUT.
import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import {
  ChevronDown, ChevronRight, ChevronsDownUp, ChevronsUpDown, Info, ListChecks, ListX, Search, ShieldCheck, Wrench,
} from "lucide-react";
import {
  ActionIcon, Alert, Anchor, Badge, Breadcrumbs, Checkbox, Divider, Group, Highlight, Paper, Select, Stack, Tabs, Text,
  TextInput, ThemeIcon, Tooltip, Tree, getTreeExpandedState, useTree, type RenderTreeNodePayload,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, ConfirmDialog, EmptyState, ErrorState, IconAction, LoadingState, PageHeader } from "@agento/ui";
import { useScopes, type Scopes } from "./admin/ScopePicker";
import { AdminOnly, message, useScope } from "./admin/shared";
import {
  changeCount, initialChecked, leavesOf, lockedLeaves, nodeState, toggle, toPayload, toTreeData,
  type LeafInfo, type RoleResources,
} from "./roleTree";

interface RoleScope { workspace_id: number | null; agent_view_id: number | null; tools: number; operations: number }
interface RoleDetail { code: string; label: string; builtin: boolean; users: number; scopes: RoleScope[] }
type ScopeKey = string; // "workspace:2", "agent_view:3"

const ADMIN_NOTE = "Administrator has user management, configuration, credentials and every module resource built in. "
  + "Tools and launching miniapps still come from what you check here.";

function RoleInfo({ role }: { role: RoleDetail }) {
  const qc = useQueryClient();
  const [label, setLabel] = useState(role.label);
  const save = useMutation({
    mutationFn: () => apiFetch<RoleDetail>(`/api/admin/roles/${role.code}`, { method: "PATCH", json: { label: label.trim() } }),
    onSuccess: () => {
      notifications.show({ message: "Role saved." });
      void qc.invalidateQueries({ queryKey: ["role", role.code] });
      void qc.invalidateQueries({ queryKey: ["roles"] });
    },
  });
  return (
    <Paper withBorder p="lg" radius="md" maw={560}>
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <Stack gap="md">
          <TextInput label="Name" required value={label} disabled={save.isPending} onChange={(e) => setLabel(e.currentTarget.value)}
            error={save.error ? message(save.error) : null} />
          <TextInput label="Code" value={role.code} readOnly variant="filled"
            description="Users and grants refer to the code, so it cannot change." />
          <Text fz="sm" c="dimmed">{role.users === 1 ? "1 user has" : `${role.users} users have`} this role.</Text>
          <Group justify="flex-end">
            <Button type="submit" variant="primary" disabled={!label.trim() || label.trim() === role.label || save.isPending}>
              Save
            </Button>
          </Group>
        </Stack>
      </form>
    </Paper>
  );
}

/** Search, expand and the tree. Keyed by scope by its parent, so a scope starts fully expanded. */
function ResourceTree({ resources, checked, locked, onChange, via, busy }: {
  resources: RoleResources; checked: Set<string>; locked: Set<string>; onChange: (next: Set<string>) => void; via: string;
  busy: boolean;
}) {
  const [search, setSearch] = useState("");
  const full = useMemo(() => toTreeData(resources, ""), [resources]);
  // Tree re-initializes on every new `data`: it must be memoized.
  const data = useMemo(() => (search ? toTreeData(resources, search) : full), [resources, search, full]);
  const [expanded, setExpanded] = useState(() => getTreeExpandedState(full, "*"));
  // Merged, not replaced: a node a search hides keeps its expanded state for when it comes back.
  const tree = useTree({ expandedState: expanded, onExpandedStateChange: (s) => setExpanded((prev) => ({ ...prev, ...s })) });
  const setAll = (on: boolean) => onChange(data.reduce((s, n) => toggle(s, n, on, locked), checked));

  const renderNode = ({ node, expanded: open, hasChildren, elementProps, level }: RenderTreeNodePayload) => {
    const leaves = leavesOf(node);
    const state = nodeState(node, checked);
    const info = (node.nodeProps ?? {}) as LeafInfo;
    const label = String(node.label);
    const box = (
      <Checkbox size="sm" aria-label={label} checked={state === "checked"} indeterminate={state === "indeterminate"}
        disabled={busy || leaves.every((v) => locked.has(v))}
        label={<Highlight highlight={search} fz="sm" fw={hasChildren ? 600 : 400} component="span">{label}</Highlight>}
        onChange={() => onChange(toggle(checked, node, state !== "checked", locked))}
        // The node's own click handler would move the focus from the checkbox to the tree item.
        wrapperProps={{ onClick: (e: React.MouseEvent) => e.stopPropagation() }} />
    );
    return (
      <Group gap="xs" wrap="nowrap" {...elementProps}>
        {hasChildren && (
          <ActionIcon variant="subtle" color="gray" size="sm" aria-label={`${open ? "Collapse" : "Expand"} ${label}`}
            aria-expanded={open} onClick={() => tree.toggleExpanded(node.value)}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </ActionIcon>
        )}
        {/* A leaf keeps the chevron's slot (same component and size), so its checkbox lines up under its group's. */}
        {!hasChildren && <ActionIcon component="span" variant="transparent" size="sm" aria-hidden />}
        {level === 1 && (
          <ThemeIcon variant="light" size="sm" radius="sm">
            {node.value === "grp:ops" ? <ShieldCheck size={14} /> : <Wrench size={14} />}
          </ThemeIcon>
        )}
        {box}
        {hasChildren && (
          <Badge variant="default" size="sm" radius="sm">
            {leaves.filter((v) => checked.has(v)).length} of {leaves.length}
          </Badge>
        )}
        {info.id && <Text fz="xs" c="dimmed" ff="monospace" truncate>{info.id}</Text>}
        {info.off && (
          <Tooltip label="A grant does not enable a tool: it stays off here until it is enabled on the Tools screen." withArrow multiline w={260}>
            <Badge variant="light" color="yellow" size="sm">Off here</Badge>
          </Tooltip>
        )}
        {info.via && (
          <Tooltip label={`Given at workspace ${via}: every agent view in it has it.`} withArrow>
            <Badge variant="light" color="blue" size="sm">Via workspace</Badge>
          </Tooltip>
        )}
        {info.builtin && (
          <Tooltip label="Built in for Administrator." withArrow>
            <Badge variant="light" color="grape" size="sm">Built in</Badge>
          </Tooltip>
        )}
      </Group>
    );
  };

  return (
    <Stack gap="sm">
      <Group gap="xs" wrap="nowrap">
        <TextInput flex={1} aria-label="Search resources" placeholder="Search tools and operations" leftSection={<Search size={16} />}
          value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
        <IconAction label="Expand all" icon={<ChevronsUpDown size={16} />} onClick={() => setExpanded(getTreeExpandedState(full, "*"))} />
        <IconAction label="Collapse all" icon={<ChevronsDownUp size={16} />} onClick={() => setExpanded({})} />
        <Divider orientation="vertical" />
        <IconAction label="Select all" icon={<ListChecks size={16} />} disabled={busy} onClick={() => setAll(true)} />
        <IconAction label="Clear" icon={<ListX size={16} />} disabled={busy} onClick={() => setAll(false)} />
      </Group>
      {data.length
        ? <Tree data={data} tree={tree} expandOnClick={false} expandOnSpace={false} levelOffset="xl" renderNode={renderNode} />
        : <EmptyState title={search ? "Nothing matches" : "Nothing to give"}>
          {search ? "No tool or operation matches the search." : "No enabled module declares a tool or an operation."}
        </EmptyState>}
    </Stack>
  );
}

const keyOf = (s: RoleScope): ScopeKey => (s.agent_view_id ? `agent_view:${s.agent_view_id}` : `workspace:${s.workspace_id}`);

function RoleResourcesTab({ role }: { role: RoleDetail }) {
  const qc = useQueryClient();
  const scopes = useScopes();
  const [, setParams] = useSearchParams();
  const url = useScope();
  const [edits, setEdits] = useState<{ at: ScopeKey; set: Set<string> } | null>(null);
  const [pending, setPending] = useState<ScopeKey | null>(null);

  const counts = new Map(role.scopes.map((s) => [keyOf(s), s.tools + s.operations]));
  const options = (kind: "workspace" | "agent_view", rows: Scopes["workspaces"] = []) => rows
    .map((r) => ({ value: `${kind}:${r.id}`, label: `${r.code} — ${r.label}` }))
    .sort((a, b) => Number(counts.has(b.value)) - Number(counts.has(a.value)));
  const groups = [
    { group: "Workspaces", items: options("workspace", scopes.data?.workspaces) },
    { group: "Agent views", items: options("agent_view", scopes.data?.agent_views) },
  ].filter((g) => g.items.length);
  const all = groups.flatMap((g) => g.items);
  // The URL scope when it names a known place; else the first place with access, else the first place.
  const fromUrl = url.ready && url.scope !== "default" ? `${url.scope}:${url.scopeId}` : null;
  const current = all.find((o) => o.value === fromUrl)?.value ?? all.find((o) => counts.has(o.value))?.value ?? all[0]?.value;
  const [kind, id] = current ? (current.split(":") as ["workspace" | "agent_view", string]) : [null, null];

  const resources = useQuery({
    queryKey: ["role-resources", role.code, current], enabled: Boolean(current),
    queryFn: ({ signal }) => apiFetch<RoleResources>(`/api/admin/roles/${role.code}/resources?scope=${kind}&scope_id=${id}`, { signal }),
  });
  const server = useMemo(() => resources.data && initialChecked(resources.data), [resources.data]);
  const locked = useMemo(() => resources.data ? lockedLeaves(resources.data) : new Set<string>(), [resources.data]);
  const checked = edits && edits.at === current ? edits.set : server;
  const changes = checked && server ? changeCount(checked, server) : 0;

  // The tree and the scope are locked while a save runs; on success only the edit state that was sent
  // is cleared, so nothing made meanwhile (a back-button scope change) is dropped.
  const save = useMutation({
    mutationFn: (sent: NonNullable<typeof edits>) => {
      const [k, i] = sent.at.split(":");
      return apiFetch(`/api/admin/roles/${role.code}/resources`, {
        method: "PUT", json: { scope: k, scope_id: Number(i), ...toPayload(sent.set, resources.data!) },
      });
    },
    onSuccess: async (_r, sent) => {
      notifications.show({ message: "Access saved." });
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["role-resources", role.code] }),
        qc.invalidateQueries({ queryKey: ["role", role.code] }),
        qc.invalidateQueries({ queryKey: ["roles"] }),
      ]);
      setEdits((e) => (e === sent ? null : e));
    },
    onError: (e) => notifications.show({ color: "red", message: message(e) }),
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

  if (scopes.isPending) return <LoadingState />;
  if (scopes.error) return <ErrorState message={message(scopes.error)} onRetry={() => void scopes.refetch()} />;
  if (!current) return <EmptyState title="No workspace yet">Access is given in a workspace or an agent view.</EmptyState>;

  return (
    <Stack gap="md">
      {role.code === "admin" && <Alert variant="light" icon={<Info size={16} />}>{ADMIN_NOTE}</Alert>}
      <Paper withBorder radius="md">
        <Group p="md" justify="space-between" wrap="wrap" gap="sm">
          <Stack gap={0}>
            <Text fw={600}>Where</Text>
            <Text fz="sm" c="dimmed">A role may use a resource only in the place it is given. A workspace gives it to every agent view in it.</Text>
          </Stack>
          <Select aria-label="Scope" data={groups} value={current} onChange={pick} allowDeselect={false} searchable miw={280}
            disabled={save.isPending}
            renderOption={({ option }) => (
              <Group flex={1} justify="space-between" wrap="nowrap" gap="xs">
                <Text fz="sm">{option.label}</Text>
                {counts.get(option.value) ? (
                  <Badge size="xs" variant="light" circle>{counts.get(option.value)}</Badge>
                ) : null}
              </Group>
            )} />
        </Group>
        <Divider />
        <Stack p="md" gap="sm">
          {resources.isPending ? <LoadingState /> : resources.error
            ? <ErrorState message={message(resources.error)} onRetry={() => void resources.refetch()} />
            : <ResourceTree key={current} resources={resources.data} checked={checked!} locked={locked} onChange={(set) => setEdits({ at: current, set })} busy={save.isPending}
              via={ws ? `${ws.code} — ${ws.label}` : "of this agent view"} />}
        </Stack>
      </Paper>
      {changes > 0 && (
        <Paper withBorder shadow="md" radius="md" p="sm" pos="sticky" bottom={0}>
          <Group justify="space-between">
            <Text fz="sm" fw={500}>{changes === 1 ? "1 change" : `${changes} changes`} not saved</Text>
            <Group gap="xs">
              <Button variant="subtle" onClick={() => setEdits(null)} disabled={save.isPending}>Reset</Button>
              <Button variant="primary" onClick={() => edits && save.mutate(edits)} disabled={save.isPending}>Save</Button>
            </Group>
          </Group>
        </Paper>
      )}
      <ConfirmDialog opened={pending !== null} title="Discard changes?" danger confirmLabel="Discard"
        onCancel={() => setPending(null)} onConfirm={() => pending && go(pending)}>
        <p>{changes === 1 ? "1 change" : `${changes} changes`} in this place are not saved.</p>
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
  const setTab = (t: string | null) => setParams((p) => {
    const next = new URLSearchParams(p);
    next.set("tab", t === "resources" ? "resources" : "info");
    return next;
  });
  return (
    <AdminOnly>
      <Stack gap="md">
        <Breadcrumbs>
          <Anchor component={Link} to="/users/roles" fz="sm">Roles</Anchor>
          <Text fz="sm" c="dimmed">{role.data?.label ?? code}</Text>
        </Breadcrumbs>
        {role.isPending ? <LoadingState /> : role.error
          ? <ErrorState message={message(role.error)} onRetry={() => void role.refetch()} />
          : (
            <>
              <PageHeader title={<Group gap="sm" component="span">{role.data.label}
                {role.data.builtin && <Badge variant="light" color="gray">Built-in</Badge>}</Group>}
                description={<Text component="span" ff="monospace" fz="sm">{role.data.code}</Text>} />
              <Tabs variant="outline" value={tab} onChange={setTab}>
                <Tabs.List>
                  <Tabs.Tab value="info">Role info</Tabs.Tab>
                  <Tabs.Tab value="resources">Role resources</Tabs.Tab>
                </Tabs.List>
                <Tabs.Panel value="info" pt="md"><RoleInfo role={role.data} /></Tabs.Panel>
                <Tabs.Panel value="resources" pt="md"><RoleResourcesTab role={role.data} /></Tabs.Panel>
              </Tabs>
            </>
          )}
      </Stack>
    </AdminOnly>
  );
}
