// The TUI config screen (framework/admin/screens/config.py): modules on the left, the fields of
// the picked module (or one of its tools) at the picked scope on the right, an editor per field.
// `tester` is "" when the field declares none. `web` holds no encryption key: a secret is never shown and never written here (plan, fact 3).
import { useState } from "react";
import {
  Alert, Badge, Grid, Group, JsonInput, Modal, MultiSelect, NavLink, NumberInput, SegmentedControl, Select, Stack, Switch,
  Text, Textarea, TextInput,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { Info } from "lucide-react";
import { useSearchParams } from "react-router";
import { apiFetch, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, ConfirmDialog, DataTable, EmptyState, ErrorState, LoadingState, PageHeader, type Column } from "@agento/ui";
import { ScopePicker } from "./ScopePicker";
import { AdminOnly, message, useConfigWrite, useScope, whole } from "./shared";

export interface Field {
  path: string; label: string; description: string; type: string; source: string; editable: boolean;
  allowed_scopes: string[]; options: { value: string; label: string }[] | null; tester: string;
  max_length: number | null; secret: boolean; is_set: boolean; value?: string | null;
}
interface TestResult { status: "ok" | "fail" | "error" | "not_configured"; code?: string; message: string }

const SOURCE_COLOR: Record<string, string> = { env: "blue", db: "green", "db:inherited": "teal", json: "gray", none: "gray" };
const TEST_COLOR: Record<TestResult["status"], string> = { ok: "green", fail: "red", error: "orange", not_configured: "gray" };
export const SECRET_HINT = "Set secrets with bin/agento config:set or the admin TUI.";

const valueCell = (f: Field) => f.secret
  ? <Text size="sm">{f.is_set ? "Set" : "Not set"}</Text>
  : <Text size="sm" lineClamp={1} style={{ overflowWrap: "anywhere" }}>{f.value ? f.value : "—"}</Text>;

/** The input for one field type; `value` is always the stored string. */
function FieldInput({ field, value, onChange, disabled }: {
  field: Field; value: string; onChange: (v: string) => void; disabled: boolean;
}) {
  const common = { label: "Value", disabled, maxLength: field.max_length ?? undefined };
  const options = field.options ?? [];
  switch (field.type) {
    case "boolean":
      return <Switch label="Enabled" disabled={disabled} checked={value === "true" || value === "1"}
        onChange={(e) => onChange(e.currentTarget.checked ? "true" : "false")} />;
    case "select":
      return <Select label="Value" disabled={disabled} data={options} value={value || null} allowDeselect={false}
        onChange={(v) => onChange(v ?? "")} />;
    case "multiselect":
      return <MultiSelect label="Value" disabled={disabled} data={options} value={value ? value.split(",") : []}
        onChange={(v) => onChange(v.join(","))} />;
    case "integer":
    case "number":
      return <NumberInput label="Value" disabled={disabled} value={value} allowDecimal={field.type === "number"}
        onChange={(v) => onChange(String(v))} />;
    case "json":
      return <JsonInput {...common} value={value} onChange={onChange} autosize minRows={4} validationError="Not valid JSON" />;
    case "textarea":
      return <Textarea {...common} value={value} onChange={(e) => onChange(e.currentTarget.value)} autosize minRows={4} />;
    default:
      return <TextInput {...common} value={value} onChange={(e) => onChange(e.currentTarget.value)} />;
  }
}

function Editor({ field, last, onClose, onRemove, onTested }: {
  field: Field; last?: TestResult; onClose: () => void; onRemove: () => void; onTested: (r: TestResult) => void;
}) {
  const { scope, scopeId } = useScope();
  const [value, setValue] = useState(field.secret ? "" : field.value ?? "");
  const write = useConfigWrite("admin-config");
  const test = useMutation({
    mutationFn: () => apiFetch<TestResult>("/api/admin/config/test", { method: "POST", json: { path: field.path, scope, scope_id: scopeId } }),
    onSuccess: (r) => {
      onTested(r);
      notifications.show({ color: TEST_COLOR[r.status], title: `Test ${r.status}`, message: r.message || field.path });
    },
    onError: (e) => notifications.show({ color: "red", message: message(e) }),
  });
  const locked = field.secret || !field.editable;
  return (
    <Stack gap="sm">
      <Text size="sm"><code>{field.path}</code> · {field.type} · <Badge variant="light" color={SOURCE_COLOR[field.source] ?? "gray"}>{field.source}</Badge></Text>
      {field.description && <Text size="sm" c="dimmed">{field.description}</Text>}
      {field.secret && <Alert color="gray" title={field.is_set ? "Set" : "Not set"}>{SECRET_HINT}</Alert>}
      {!field.secret && !field.editable && (
        <Alert color="yellow">Read-only at this scope. It is configurable at: {field.allowed_scopes.join(", ") || "none"}.</Alert>
      )}
      <FieldInput field={field} value={value} onChange={setValue} disabled={locked} />
      {last && <Text size="sm">Last test: <Badge variant="light" color={TEST_COLOR[last.status]}>{last.status}</Badge> {last.message}</Text>}
      <Group justify="space-between">
        <Group gap="xs">
          {field.tester && <Button disabled={test.isPending} onClick={() => test.mutate()}>Test</Button>}
          {field.source === "db" && !field.secret && <Button variant="danger" onClick={onRemove}>Remove override</Button>}
        </Group>
        <Group gap="xs">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={locked || write.isPending}
            onClick={() => write.mutate([{ path: field.path, value }], { onSuccess: onClose })}>Save</Button>
        </Group>
      </Group>
    </Stack>
  );
}

function Fields({ module, tool }: { module: string; tool: string | null }) {
  const qc = useQueryClient();
  const { scope, scopeId, ready, query } = useScope();
  const [filter, setFilter] = useState("");
  const [only, setOnly] = useState("all");
  const [editing, setEditing] = useState<Field | null>(null);
  const [removing, setRemoving] = useState<Field | null>(null);
  const [last, setLast] = useState<Record<string, TestResult>>({});
  const fields = useQuery({
    queryKey: ["admin-config", module, query], enabled: ready,
    queryFn: ({ signal }) => apiFetch<Field[]>(`/api/admin/config?module=${encodeURIComponent(module)}&${query}`, { signal }),
  });
  const remove = useMutation({
    mutationFn: (f: Field) => apiFetch("/api/admin/config", { method: "DELETE", json: { path: f.path, scope, scope_id: scopeId } }),
    onSuccess: (_r, f) => { notifications.show({ message: `Override of ${f.path} removed.` }); setRemoving(null); },
    onError: (e) => notifications.show({ color: "red", message: message(e) }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["admin-config"] }),
  });

  if (!ready) return <EmptyState title="Choose a scope" />;
  const needle = filter.trim().toLowerCase();
  const prefix = tool ? `${module}/tools/${tool}/` : null;
  const rows = fields.data?.filter((f) => (!prefix || f.path.startsWith(prefix))
    && (only === "all" || f.source === "db")
    && (!needle || `${f.path} ${f.label}`.toLowerCase().includes(needle)));
  const columns: Column<Field>[] = [
    { id: "field", header: "Field", sortValue: (f) => f.path, cell: (f) => (
      <div><Text size="sm" fw={500}>{f.label}</Text><Text size="xs" c="dimmed"><code>{f.path}</code></Text></div>
    ) },
    { id: "value", header: "Value", cell: valueCell },
    { id: "source", header: "Source", sortValue: (f) => f.source,
      cell: (f) => whole(<Badge variant="light" color={SOURCE_COLOR[f.source] ?? "gray"}>{f.source}</Badge>) },
    { id: "actions", header: "Actions", cell: (f) => <Button variant="subtle" onClick={() => setEditing(f)}>Edit</Button> },
  ];
  return (
    <Stack gap="sm">
      <Group gap="sm" align="flex-end">
        <TextInput aria-label="Filter fields" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.currentTarget.value)} />
        <SegmentedControl aria-label="Show" value={only} onChange={setOnly}
          data={[{ value: "all", label: "All" }, { value: "overrides", label: "Overrides" }]} />
      </Group>
      <DataTable caption={`Config of ${tool ?? module}`} columns={columns} rows={rows} rowKey={(f) => f.path} onRowActivate={setEditing}
        loading={fields.isPending} error={fields.error ? message(fields.error) : null} onRetry={() => void fields.refetch()}
        emptyTitle="No fields" />
      <Modal opened={editing !== null} onClose={() => setEditing(null)} title={editing?.label ?? ""} size="lg" centered
        closeButtonProps={{ "aria-label": "Close" }}>
        {editing && (
          <Editor key={editing.path} field={editing} last={last[editing.path]} onClose={() => setEditing(null)}
            onTested={(r) => setLast((m) => ({ ...m, [editing.path]: r }))}
            onRemove={() => { setRemoving(editing); setEditing(null); }} />
        )}
      </Modal>
      <ConfirmDialog opened={removing !== null} title="Remove this override?" danger confirmLabel="Remove override"
        busy={remove.isPending} onCancel={() => setRemoving(null)} onConfirm={() => removing && remove.mutate(removing)}>
        <p>{removing?.path} falls back to the value of the parent scope, ENV or config.json.</p>
      </ConfirmDialog>
    </Stack>
  );
}

export function Config() {
  const [params, setParams] = useSearchParams();
  const modules = useQuery({
    queryKey: ["admin-config-modules"],
    queryFn: ({ signal }) => apiFetch<{ name: string; tools: string[] }[]>("/api/admin/config/modules", { signal }),
  });
  const list = modules.data ?? [];
  const module = params.get("module") ?? list[0]?.name ?? null;
  const tool = params.get("tool");
  const pick = (m: string, t?: string) => setParams((p) => {
    const n = new URLSearchParams(p);
    n.set("module", m);
    if (t) n.set("tool", t); else n.delete("tool");
    return n;
  });
  return (
    <AdminOnly>
      <div className="ag-stack">
        <PageHeader title="Config" description="Module and tool settings at a scope." />
        <Alert color="blue" icon={<Info size={16} aria-hidden />}>
          CONFIG__* ENV overrides are not visible here. <code>bin/agento config:resolve &lt;path&gt;</code> shows the
          effective value.
        </Alert>
        <ScopePicker />
        {modules.isPending ? <LoadingState /> : modules.error
          ? <ErrorState message={message(modules.error)} onRetry={() => void modules.refetch()} />
          : !module ? <EmptyState title="No configurable modules" /> : (
            <Grid>
              <Grid.Col span={{ base: 12, md: 3 }}>
                <nav aria-label="Modules">
                  {list.map((m) => (
                    <NavLink key={m.name} label={m.name} active={m.name === module && !tool} defaultOpened={m.name === module}
                      onClick={() => pick(m.name)} childrenOffset="md">
                      {m.tools.length ? m.tools.map((t) => (
                        <NavLink key={t} label={t} active={m.name === module && t === tool} onClick={() => pick(m.name, t)} />
                      )) : undefined}
                    </NavLink>
                  ))}
                </nav>
              </Grid.Col>
              <Grid.Col span={{ base: 12, md: 9 }}><Fields module={module} tool={tool} /></Grid.Col>
            </Grid>
          )}
      </div>
    </AdminOnly>
  );
}
