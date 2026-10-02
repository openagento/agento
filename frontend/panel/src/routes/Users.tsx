// Users and grants (PRD E2 §5): admin only. The API enforces the role; this screen only
// renders what the routes answer.
import { useState } from "react";
import { Tabs } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, ApiError, useMutation, useQuery, useQueryClient, useSession, type User } from "@agento/api";
import {
  Button, ConfirmDialog, DataTable, EmptyState, FormSection, PageHeader, SelectField, StatusBadge, TextField,
  type Column,
} from "@agento/ui";
import { Grants } from "./Grants";

const ROLES = [{ value: "user", label: "user" }, { value: "admin", label: "admin" }];
const message = (e: unknown) => (e instanceof ApiError ? e.message : "The request failed.");

function CreateUser() {
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [role, setRole] = useState("user");
  const [password, setPassword] = useState("");
  const create = useMutation({
    mutationFn: () => apiFetch<User>("/api/admin/users", {
      method: "POST", json: { username, role, ...(password ? { password } : {}) },
    }),
    onSuccess: (u) => {
      notifications.show({ message: `User ${u.username} created.` });
      setUsername(""); setPassword("");
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
    onSettled: () => setPassword(""),
  });
  return (
    <FormSection title="Add a user" onSubmit={() => create.mutate()} error={create.error ? message(create.error) : null}>
      <TextField label="User name" name="new-username" required value={username} onChange={(e) => setUsername(e.target.value)} />
      <SelectField label="Role" name="new-role" options={ROLES} value={role} onChange={(e) => setRole(e.target.value)} />
      <TextField label="Password" name="new-password" type="password" autoComplete="new-password"
        hint="Leave empty to create the user without a password." value={password}
        onChange={(e) => setPassword(e.target.value)} />
      <div className="ag-row"><Button type="submit" variant="primary" disabled={create.isPending}>Add user</Button></div>
    </FormSection>
  );
}

function UserTable() {
  const me = useSession();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: ({ signal }) => apiFetch<User[]>("/api/admin/users", { signal }) });
  const [deactivate, setDeactivate] = useState<User | null>(null);
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: Partial<Pick<User, "role" | "is_active">> }) =>
      apiFetch<User>(`/api/admin/users/${id}`, { method: "PATCH", json: patch }),
    onSuccess: (u) => {
      notifications.show({ message: `User ${u.username} updated.` });
      setDeactivate(null);
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e) => notifications.show({ color: "red", message: message(e) }),
  });

  const columns: Column<User>[] = [
    { id: "username", header: "User name", cell: (u) => u.username, sortValue: (u) => u.username },
    { id: "role", header: "Role", sortValue: (u) => u.role,
      cell: (u) => <StatusBadge tone={u.role === "admin" ? "info" : "neutral"}>{u.role}</StatusBadge> },
    { id: "active", header: "Status", sortValue: (u) => (u.is_active ? 1 : 0),
      cell: (u) => <StatusBadge tone={u.is_active ? "succeeded" : "neutral"}>{u.is_active ? "Active" : "Inactive"}</StatusBadge> },
    { id: "actions", header: "Actions", cell: (u) => u.id === me?.id ? <span className="ag-muted">You</span> : (
      <div className="ag-row">
        <Button variant="subtle" disabled={update.isPending}
          onClick={() => update.mutate({ id: u.id, patch: { role: u.role === "admin" ? "user" : "admin" } })}>
          Make {u.role === "admin" ? "user" : "admin"}
        </Button>
        {u.is_active
          ? <Button variant="danger" onClick={() => setDeactivate(u)}>Deactivate</Button>
          : <Button onClick={() => update.mutate({ id: u.id, patch: { is_active: true } })}>Activate</Button>}
      </div>
    ) },
  ];

  return (
    <>
      <DataTable caption="Users" columns={columns} rows={users.data} rowKey={(u) => String(u.id)}
        loading={users.isPending} error={users.error ? message(users.error) : null} onRetry={() => void users.refetch()}
        emptyTitle="No users" />
      <ConfirmDialog opened={deactivate !== null} title="Deactivate this user?" danger confirmLabel="Deactivate"
        busy={update.isPending} onCancel={() => setDeactivate(null)}
        onConfirm={() => deactivate && update.mutate({ id: deactivate.id, patch: { is_active: false } })}>
        <p>{deactivate?.username} is signed out and cannot sign in until a user is activated again.</p>
      </ConfirmDialog>
    </>
  );
}

export function Users() {
  const me = useSession();
  if (me?.role !== "admin") return <EmptyState title="Not available">Only an administrator manages users.</EmptyState>;
  return (
    <div className="ag-stack">
      <PageHeader title="Users" description="Who can sign in to the panel, and what each role may do." />
      <Tabs defaultValue="users">
        <Tabs.List>
          <Tabs.Tab value="users">Users</Tabs.Tab>
          <Tabs.Tab value="grants">Grants</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="users" pt="md"><div className="ag-stack"><UserTable /><CreateUser /></div></Tabs.Panel>
        <Tabs.Panel value="grants" pt="md"><Grants /></Tabs.Panel>
      </Tabs>
    </div>
  );
}
