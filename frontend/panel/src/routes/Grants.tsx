// Grants (plan G1): a place and a name are picked from lists, never typed as ids, so the sheet
// cannot send what the API refuses (one scope, a declared tool, a grantable operation).
import { useState } from "react";
import { Alert, Group, Modal, MultiSelect, SegmentedControl, Select, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, ConfirmDialog, DataTable, StatusBadge, type Column } from "@agento/ui";
import { useScopes, type Scopes } from "./admin/ScopePicker";
import { message, whole } from "./admin/shared";

interface Grant {
  id: number; role: string; grant_kind: string; name: string;
  workspace_id: number | null; agent_view_id: number | null; created_at: string | null;
}
type Place = "workspace" | "agent_view";
type What = "tool" | "operation";
type Toolsets = { toolset: string; tools: { name: string; enabled: boolean }[] }[];

const PLACE_LABEL: Record<Place, string> = { workspace: "Workspace", agent_view: "Agent view" };
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** "Agent view · av — AV"; the raw id only when the scope list does not know the row. */
function placeOf(g: Grant, scopes: Scopes | undefined): string {
  const [kind, id, rows]: [Place, number | null, Scopes["workspaces"] | undefined] = g.agent_view_id
    ? ["agent_view", g.agent_view_id, scopes?.agent_views] : ["workspace", g.workspace_id, scopes?.workspaces];
  const row = rows?.find((r) => r.id === id);
  return `${PLACE_LABEL[kind]} · ${row ? `${row.code} — ${row.label}` : id}`;
}

function GiveAccess({ opened, onClose, grants }: { opened: boolean; onClose: () => void; grants: Grant[] }) {
  const qc = useQueryClient();
  const scopes = useScopes();
  const options = useQuery({
    queryKey: ["grant-options"], enabled: opened,
    queryFn: ({ signal }) => apiFetch<{ roles: string[]; operations: string[] }>("/api/admin/grants/options", { signal }),
  });
  const [role, setRole] = useState("user");
  const [place, setPlace] = useState<Place>("workspace");
  const [scopeId, setScopeId] = useState<string | null>(null);
  const [what, setWhat] = useState<What>("tool");
  const [names, setNames] = useState<string[]>([]);
  const [failed, setFailed] = useState<string | null>(null);
  const query = `scope=${place}&scope_id=${scopeId}`;
  const tools = useQuery({
    queryKey: ["admin-tools", query], enabled: opened && what === "tool" && scopeId !== null,
    queryFn: ({ signal }) => apiFetch<Toolsets>(`/api/admin/tools?${query}`, { signal }),
  });

  const id = Number(scopeId);
  const has = new Set(grants.filter((g) => g.role === role && g.grant_kind === what
    && (place === "agent_view" ? g.agent_view_id === id && g.workspace_id === null
      : g.workspace_id === id && g.agent_view_id === null)).map((g) => g.name));
  const data = scopeId === null ? [] : what === "tool"
    ? (tools.data ?? []).map((t) => ({
      group: t.toolset,
      items: t.tools.filter((x) => !has.has(x.name)).map((x) => ({ value: x.name, label: x.enabled ? x.name : `${x.name} (off here)` })),
    })).filter((g) => g.items.length)
    : (options.data?.operations ?? []).filter((o) => !has.has(o)).map((o) => ({ value: o, label: o }));
  const rows = place === "workspace" ? scopes.data?.workspaces : scopes.data?.agent_views;

  // One POST per name, in order. A refusal stops the batch and names the item; the ones before it stay.
  const give = useMutation({
    mutationFn: async () => {
      for (const [i, name] of names.entries()) {
        try {
          await apiFetch("/api/admin/grants", { method: "POST", json: { role, kind: what, name, [`${place}_id`]: id } });
        } catch (e) {
          setNames(names.slice(i));
          throw new Error(`Gave ${i} of ${names.length}. ${name}: ${message(e)}`);
        }
      }
      return names.length;
    },
    onMutate: () => setFailed(null),
    onSuccess: (n) => { notifications.show({ message: n === 1 ? "Access given." : `Access given to ${n}.` }); setNames([]); },
    onError: (e) => setFailed(e.message),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["grants"] }),
  });

  return (
    <Modal opened={opened} onClose={onClose} title="Give access" centered size="lg" closeButtonProps={{ "aria-label": "Close" }}>
      <Stack gap="md">
        <Stack gap="xs">
          <Text size="sm" fw={500}>Who</Text>
          <SegmentedControl aria-label="Who" value={role} onChange={(v) => { setRole(v); setNames([]); }}
            data={(options.data?.roles ?? []).map((r) => ({ value: r, label: cap(r) }))} />
        </Stack>
        <Stack gap="xs">
          <Text size="sm" fw={500}>Where</Text>
          <SegmentedControl aria-label="Where" value={place}
            onChange={(v) => { setPlace(v as Place); setScopeId(null); setNames([]); }}
            data={(["workspace", "agent_view"] as const).map((p) => ({ value: p, label: PLACE_LABEL[p] }))} />
          <Select aria-label={PLACE_LABEL[place]} placeholder="Choose…" searchable required allowDeselect={false}
            data={(rows ?? []).map((r) => ({ value: String(r.id), label: `${r.code} — ${r.label}` }))}
            value={scopeId} onChange={(v) => { setScopeId(v); setNames([]); }} />
        </Stack>
        <Stack gap="xs">
          <Text size="sm" fw={500}>What</Text>
          <SegmentedControl aria-label="What" value={what} onChange={(v) => { setWhat(v as What); setNames([]); }}
            data={[{ value: "tool", label: "Tools" }, { value: "operation", label: "Operations" }]} />
          <MultiSelect aria-label={what === "tool" ? "Tools" : "Operations"} searchable data={data} value={names}
            onChange={setNames} disabled={scopeId === null} placeholder={scopeId === null ? "Choose a place first" : "Choose…"}
            description={what === "tool" ? "A grant does not enable a tool: a tool marked (off here) stays off until it is enabled." : undefined}
            nothingFoundMessage="Nothing left to give here" />
        </Stack>
        {failed && <Alert color="red">{failed}</Alert>}
        <Group justify="flex-end">
          <Button onClick={onClose}>Close</Button>
          <Button variant="primary" disabled={scopeId === null || !names.length || give.isPending} onClick={() => give.mutate()}>
            Give access to {names.length}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function Grants() {
  const qc = useQueryClient();
  const scopes = useScopes();
  const grants = useQuery({ queryKey: ["grants"], queryFn: ({ signal }) => apiFetch<Grant[]>("/api/admin/grants", { signal }) });
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<Grant | null>(null);
  const remove = useMutation({
    mutationFn: (id: number) => apiFetch(`/api/admin/grants/${id}`, { method: "DELETE" }),
    onSuccess: () => { notifications.show({ message: "Access removed." }); setRemoving(null); },
    onError: (e) => notifications.show({ color: "red", message: message(e) }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["grants"] }),
  });

  const place = (g: Grant) => placeOf(g, scopes.data);
  const rows = grants.data && [...grants.data].sort((a, b) => place(a).localeCompare(place(b)) || a.name.localeCompare(b.name));
  const addButton = <Button variant="primary" onClick={() => setAdding(true)}>Add access</Button>;
  const columns: Column<Grant>[] = [
    { id: "role", header: "Role", sortValue: (g) => g.role,
      cell: (g) => whole(<StatusBadge tone={g.role === "admin" ? "info" : "neutral"}>{cap(g.role)}</StatusBadge>) },
    { id: "access", header: "Access", sortValue: (g) => g.name, cell: (g) => (
      <div><Text size="sm" fw={500}>{g.name}</Text><Text size="xs" c="dimmed">{g.grant_kind}</Text></div>
    ) },
    { id: "where", header: "Where", sortValue: place, cell: place },
    { id: "remove", header: "Actions", cell: (g) => <Button variant="danger" onClick={() => setRemoving(g)}>Remove</Button> },
  ];

  return (
    <div className="ag-stack">
      {rows?.length ? (
        <Group justify="space-between">
          <Text size="sm" c="dimmed">A role can use only what it is given, and only where it is given.</Text>
          {addButton}
        </Group>
      ) : null}
      <DataTable caption="Grants" columns={columns} rows={rows} rowKey={(g) => String(g.id)}
        loading={grants.isPending} error={grants.error ? message(grants.error) : null}
        onRetry={() => void grants.refetch()} emptyTitle="No one has access yet" emptyText={addButton} />
      <GiveAccess opened={adding} onClose={() => setAdding(false)} grants={grants.data ?? []} />
      <ConfirmDialog opened={removing !== null} title="Remove this access?" danger confirmLabel="Remove"
        busy={remove.isPending} onCancel={() => setRemoving(null)} onConfirm={() => removing && remove.mutate(removing.id)}>
        {removing && (
          <p>
            {cap(removing.role)} can no longer use {removing.name} in {place(removing)}.
            {removing.name === "artifact.launch" && " Open launches there end."}
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}
