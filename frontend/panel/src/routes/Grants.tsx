import { useState } from "react";
import { notifications } from "@mantine/notifications";
import { apiFetch, ApiError, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, DataTable, FormSection, SelectField, TextField, type Column } from "@agento/ui";

interface Grant {
  id: number; role: string; grant_kind: string; name: string;
  workspace_id: number | null; agent_view_id: number | null; created_at: string | null;
}

const message = (e: unknown) => (e instanceof ApiError ? e.message : "The request failed.");
/** An empty scope field is "not set"; anything else must be a positive id, or the form is refused:
 *  a mistyped id must never fall back to a grant that applies everywhere. */
export function scopeId(v: string): number | undefined | null {
  const t = v.trim();
  if (t === "") return undefined;
  return /^[1-9][0-9]*$/.test(t) && Number.isSafeInteger(Number(t)) ? Number(t) : null;
}

export function Grants() {
  const qc = useQueryClient();
  const grants = useQuery({ queryKey: ["grants"], queryFn: ({ signal }) => apiFetch<Grant[]>("/api/admin/grants", { signal }) });
  const [form, setForm] = useState({ role: "user", kind: "tool", name: "", workspace: "", view: "" });
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const pick = (k: keyof typeof form) => (value: string) => setForm({ ...form, [k]: value });
  const done = (text: string) => {
    notifications.show({ message: text });
    void qc.invalidateQueries({ queryKey: ["grants"] });
  };
  const [invalid, setInvalid] = useState<string | null>(null);
  const add = useMutation({
    mutationFn: (scope: { workspace_id?: number; agent_view_id?: number }) => apiFetch("/api/admin/grants", {
      method: "POST", json: { role: form.role, kind: form.kind, name: form.name, ...scope },
    }),
    onSuccess: () => { setForm({ ...form, name: "" }); done("Grant added."); },
  });
  const submit = () => {
    const workspace_id = scopeId(form.workspace);
    const agent_view_id = scopeId(form.view);
    if (workspace_id === null || agent_view_id === null) {
      setInvalid("A workspace or agent view id is a positive whole number.");
      return;
    }
    setInvalid(null);
    add.mutate({ workspace_id, agent_view_id });
  };
  const remove = useMutation({
    mutationFn: (id: number) => apiFetch(`/api/admin/grants/${id}`, { method: "DELETE" }),
    onSuccess: () => done("Grant removed."),
    onError: (e) => notifications.show({ color: "red", message: message(e) }),
  });

  const scope = (g: Grant) => g.agent_view_id ? `agent view ${g.agent_view_id}`
    : g.workspace_id ? `workspace ${g.workspace_id}` : "everywhere";
  const columns: Column<Grant>[] = [
    { id: "role", header: "Role", cell: (g) => g.role, sortValue: (g) => g.role },
    { id: "kind", header: "Kind", cell: (g) => g.grant_kind, sortValue: (g) => g.grant_kind },
    { id: "name", header: "Name", cell: (g) => g.name, sortValue: (g) => g.name },
    { id: "scope", header: "Scope", cell: scope },
    { id: "remove", header: "Actions",
      cell: (g) => <Button variant="danger" disabled={remove.isPending} onClick={() => remove.mutate(g.id)}>Remove</Button> },
  ];

  return (
    <div className="ag-stack">
      <DataTable caption="Grants" columns={columns} rows={grants.data} rowKey={(g) => String(g.id)}
        loading={grants.isPending} error={grants.error ? message(grants.error) : null}
        onRetry={() => void grants.refetch()} emptyTitle="No grants" emptyText="A role can do nothing that is not granted." />
      <FormSection title="Add a grant" onSubmit={submit} error={invalid ?? (add.error ? message(add.error) : null)}>
        <SelectField label="Role" name="grant-role" value={form.role} onChange={pick("role")}
          options={[{ value: "user", label: "user" }, { value: "admin", label: "admin" }]} />
        <SelectField label="Kind" name="grant-kind" value={form.kind} onChange={pick("kind")}
          options={[{ value: "tool", label: "tool" }, { value: "operation", label: "operation" }]} />
        <TextField label="Name" name="grant-name" required value={form.name} onChange={set("name")}
          hint="A declared tool name, or an operation such as artifact.launch." />
        <TextField label="Workspace id" name="grant-workspace" inputMode="numeric" value={form.workspace}
          onChange={set("workspace")} hint="Optional. Leave both ids empty for every scope." />
        <TextField label="Agent view id" name="grant-view" inputMode="numeric" value={form.view} onChange={set("view")} />
        <div className="ag-row"><Button type="submit" variant="primary" disabled={add.isPending}>Add grant</Button></div>
      </FormSection>
    </div>
  );
}
