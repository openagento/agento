// The TUI config screen (framework/admin/screens/config.py): modules on the left, the fields of
// the picked module (or one of its tools) at the picked scope on the right, an editor per field.
// `tester` is "" when the field declares none. A secret is never shown and never written here.
import { useState } from "react";
import {
  Alert, Badge, Grid, Group, JsonInput, Modal, MultiSelect, NavLink, NumberInput, SegmentedControl, Select, Stack, Switch,
  Text, Textarea, TextInput,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useSearchParams } from "react-router";
import { Pencil } from "lucide-react";
import { apiFetch, refreshDisplay, useErrorText, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  Button, ConfirmDialog, DataTable, EmptyState, ErrorState, IconAction, LoadingState, PageHeader, useLookup, useT, type Column, type Translate,
} from "@agento/ui";
import { ScopePicker } from "./ScopePicker";
import { useConfigWrite, useScope, whole } from "./shared";

export interface Field {
  path: string; label: string; description: string; type: string; source: string; editable: boolean;
  allowed_scopes: string[]; options: { value: string; label: string; label_key?: string }[] | null; tester: string;
  max_length: number | null; secret: boolean; is_set: boolean; value?: string | null;
}
interface TestResult { status: "ok" | "fail" | "error" | "not_configured"; code?: string; message: string }

const SOURCE_COLOR: Record<string, string> = { env: "blue", db: "green", "db:inherited": "teal", json: "gray", none: "gray" };
const TEST_COLOR: Record<TestResult["status"], string> = { ok: "green", fail: "red", error: "orange", not_configured: "gray" };

/** No module declares a title (module.json has `name` only): "agent_view" → "Agent View". */
const moduleTitle = (name: string) => name.split("_").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");

const testTexts = (t: Translate): Record<TestResult["status"], { title: string; status: string }> => ({
  ok: { title: t("config.test.title.ok"), status: t("config.test.status.ok") },
  fail: { title: t("config.test.title.fail"), status: t("config.test.status.fail") },
  error: { title: t("config.test.title.error"), status: t("config.test.status.error") },
  not_configured: { title: t("config.test.title.not_configured"), status: t("config.test.status.not_configured") },
});

/** An `error` result names a code and the text comes from a catalog (the field's module, then
 *  `panel`). The text of any other result is the probe's own and shows as sent. */
function useTestMessage() {
  const lookup = useLookup();
  const t = useT("panel");
  return (field: Field, r: TestResult) => r.status !== "error"
    ? r.message
    : lookup(field.path.split("/")[0], `error.${r.code}`) ?? lookup("panel", `error.${r.code}`) ?? t("config.test.unchecked");
}

/** The texts a manifest gives (`system.json`, the module name) in the panel's language. Their
 *  English source is the manifest, so a key that no catalog holds shows the server's text. */
function useManifestTexts() {
  const lookup = useLookup();
  const derived = (f: Field, part: string) => {
    const [module, ...fieldPath] = f.path.split("/");
    return lookup(module, `config.${fieldPath.join("/")}.${part}`);
  };
  return {
    label: (f: Field) => derived(f, "label") ?? f.label,
    description: (f: Field) => derived(f, "description") ?? f.description,
    options: (f: Field) => (f.options ?? []).map((o) => ({
      value: o.value, label: derived(f, `option.${o.value}`) ?? (o.label_key ? lookup("panel", `config.option.${o.label_key}`) : undefined) ?? o.label,
    })),
    moduleTitle: (name: string) => lookup(name, "module.title") ?? moduleTitle(name),
  };
}

/** The input for one field type; `value` is always the stored string. */
function FieldInput({ field, value, onChange, disabled }: {
  field: Field; value: string; onChange: (v: string) => void; disabled: boolean;
}) {
  const t = useT("panel");
  const texts = useManifestTexts();
  // A stored secret shows a mask, never its value: the placeholder is not a value, so Save cannot write it.
  const common = { label: t("config.value"), disabled, maxLength: field.max_length ?? undefined,
    placeholder: field.secret && field.is_set ? "****" : undefined };
  const options = texts.options(field);
  switch (field.type) {
    case "boolean":
      return <Switch label={t("config.enabled")} disabled={disabled} checked={value === "true" || value === "1"}
        onChange={(e) => onChange(e.currentTarget.checked ? "true" : "false")} />;
    case "select":
      return <Select label={t("config.value")} disabled={disabled} data={options} value={value || null} allowDeselect={false} searchable
        onChange={(v) => onChange(v ?? "")} />;
    case "multiselect":
      return <MultiSelect label={t("config.value")} disabled={disabled} data={options} value={value ? value.split(",") : []}
        onChange={(v) => onChange(v.join(","))} />;
    case "integer":
    case "number":
      return <NumberInput label={t("config.value")} disabled={disabled} value={value} allowDecimal={field.type === "number"}
        onChange={(v) => onChange(String(v))} />;
    case "json":
      return <JsonInput {...common} value={value} onChange={onChange} autosize minRows={4} validationError={t("config.invalid_json")} />;
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
  const t = useT("panel");
  const texts = useManifestTexts();
  const errorText = useErrorText("panel");
  const testMessage = useTestMessage();
  const description = texts.description(field);
  const test = useMutation({
    mutationFn: () => apiFetch<TestResult>("/api/admin/config/test", { method: "POST", json: { path: field.path, scope, scope_id: scopeId } }),
    onSuccess: (r) => {
      onTested(r);
      notifications.show({ color: TEST_COLOR[r.status], title: testTexts(t)[r.status].title, message: testMessage(field, r) || field.path });
    },
    onError: (e) => notifications.show({ color: "red", message: errorText(e, "config.test.failed") }),
  });
  const locked = field.secret || !field.editable;
  return (
    <Stack gap="sm">
      <Text size="sm"><code>{field.path}</code> · {field.type} · <Badge variant="light" color={SOURCE_COLOR[field.source] ?? "gray"}>{field.source}</Badge></Text>
      {description && <Text size="sm" c="dimmed">{description}</Text>}
      {field.secret && <Alert color="gray" title={field.is_set ? t("config.secret.set") : t("config.secret.not_set")}>{t("config.secret.hint")}</Alert>}
      {!field.secret && !field.editable && (
        <Alert color="yellow">{t("config.read_only", { scopes: field.allowed_scopes.join(", ") || t("config.no_scope") })}</Alert>
      )}
      <FieldInput field={field} value={value} onChange={setValue} disabled={locked} />
      {last && <Text size="sm">{t("config.test.last")} <Badge variant="light" color={TEST_COLOR[last.status]}>{testTexts(t)[last.status].status}</Badge> {testMessage(field, last)}</Text>}
      <Group justify="space-between">
        <Group gap="xs">
          {field.tester && <Button disabled={test.isPending} onClick={() => test.mutate()}>{t("config.test.run")}</Button>}
          {field.source === "db" && !field.secret && <Button variant="danger" onClick={onRemove}>{t("config.remove_override")}</Button>}
        </Group>
        <Group gap="xs">
          <Button onClick={onClose}>{t("action.cancel")}</Button>
          <Button variant="primary" disabled={locked || write.isPending}
            onClick={() => write.mutate([{ path: field.path, value }], { onSuccess: onClose })}>{t("action.save")}</Button>
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
  const t = useT("panel");
  const texts = useManifestTexts();
  const errorText = useErrorText("panel");
  const fields = useQuery({
    queryKey: ["admin-config", module, query], enabled: ready,
    queryFn: ({ signal }) => apiFetch<Field[]>(`/api/admin/config?module=${encodeURIComponent(module)}&${query}`, { signal }),
  });
  const remove = useMutation({
    mutationFn: (f: Field) => apiFetch("/api/admin/config", { method: "DELETE", json: { path: f.path, scope, scope_id: scopeId } }),
    onSuccess: (_r, f) => { notifications.show({ message: t("config.override_removed", { path: f.path }) }); setRemoving(null); },
    onError: (e) => notifications.show({ color: "red", message: errorText(e, "config.remove_failed") }),
    onSettled: () => { void qc.invalidateQueries({ queryKey: ["admin-config"] }); void refreshDisplay(); },
  });

  if (!ready) return <EmptyState title={t("config.choose_scope")} />;
  const needle = filter.trim().toLowerCase();
  const prefix = tool ? `${module}/tools/${tool}/` : null;
  const rows = fields.data?.filter((f) => (!prefix || f.path.startsWith(prefix))
    && (only === "all" || f.source === "db")
    && (!needle || `${f.path} ${texts.label(f)}`.toLowerCase().includes(needle)));
  const columns: Column<Field>[] = [
    { id: "field", header: t("config.column.field"), sortValue: (f) => f.path, cell: (f) => (
      <div><Text size="sm" fw={500}>{texts.label(f)}</Text><Text size="xs" c="dimmed"><code>{f.path}</code></Text></div>
    ) },
    { id: "value", header: t("config.column.value"), cell: (f) => (f.secret
      ? <Text size="sm">{f.is_set ? t("config.secret.set") : t("config.secret.not_set")}</Text>
      : <Text size="sm" lineClamp={1} style={{ overflowWrap: "anywhere" }}>{f.value ? f.value : "—"}</Text>) },
    { id: "source", header: t("config.column.source"), sortValue: (f) => f.source,
      cell: (f) => whole(<Badge variant="light" color={SOURCE_COLOR[f.source] ?? "gray"}>{f.source}</Badge>) },
    { id: "actions", header: t("config.column.actions"), cell: (f) => <IconAction label={t("action.edit")} icon={<Pencil size={16} />} onClick={() => setEditing(f)} /> },
  ];
  return (
    <Stack gap="sm">
      <Group gap="sm" align="flex-end">
        <TextInput aria-label={t("config.filter.label")} placeholder={t("config.filter.placeholder")} value={filter} onChange={(e) => setFilter(e.currentTarget.value)} />
        <SegmentedControl aria-label={t("config.show.label")} value={only} onChange={setOnly}
          data={[{ value: "all", label: t("config.show.all") }, { value: "overrides", label: t("config.show.overrides") }]} />
      </Group>
      <DataTable caption={t("config.caption", { name: tool ?? module })} columns={columns} rows={rows} rowKey={(f) => f.path} onRowActivate={setEditing}
        loading={fields.isPending} error={fields.error ? errorText(fields.error, "config.load_failed") : null} onRetry={() => void fields.refetch()}
        emptyTitle={t("config.no_fields")} />
      <Modal opened={editing !== null} onClose={() => setEditing(null)} title={editing ? texts.label(editing) : ""} size="lg" centered
        closeButtonProps={{ "aria-label": t("action.close") }}>
        {editing && (
          <Editor key={editing.path} field={editing} last={last[editing.path]} onClose={() => setEditing(null)}
            onTested={(r) => setLast((m) => ({ ...m, [editing.path]: r }))}
            onRemove={() => { setRemoving(editing); setEditing(null); }} />
        )}
      </Modal>
      <ConfirmDialog opened={removing !== null} title={t("config.remove.title")} danger confirmLabel={t("config.remove_override")}
        busy={remove.isPending} onCancel={() => setRemoving(null)} onConfirm={() => removing && remove.mutate(removing)}>
        <p>{t("config.remove.text", { path: removing?.path ?? "" })}</p>
      </ConfirmDialog>
    </Stack>
  );
}

export function Config() {
  const [params, setParams] = useSearchParams();
  const t = useT("panel");
  const texts = useManifestTexts();
  const errorText = useErrorText("panel");
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
    <div className="ag-stack">
      <PageHeader title={t("config.title")} description={t("config.description")} />
      <ScopePicker />
      {modules.isPending ? <LoadingState /> : modules.error
        ? <ErrorState message={errorText(modules.error, "config.load_failed")} onRetry={() => void modules.refetch()} />
        : !module ? <EmptyState title={t("config.no_modules")} /> : (
          <Grid>
            <Grid.Col span={{ base: 12, md: 3 }}>
              <nav aria-label={t("config.modules")}>
                {list.map((m) => (
                  <NavLink key={m.name} label={texts.moduleTitle(m.name)} active={m.name === module && !tool} defaultOpened={m.name === module}
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
  );
}
