// Users and roles (PRD E2 §5). The API checks the leaves; this screen only renders what the
// routes answer.
import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";
import { UserCheck, UserX } from "lucide-react";
import { Avatar, Group, Select, Tabs, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useCan, useErrorText, useMutation, useQuery, useQueryClient, useSession, type User } from "@agento/api";
import {
  Button, ConfirmDialog, IconAction, DataTable, FormSection, PageHeader, SelectField, StatusBadge, TextField, useT,
  type Column,
} from "@agento/ui";
import { Roles, useRoles } from "./Roles";

const useRoleOptions = () => (useRoles().data ?? []).map((r) => ({ value: r.code, label: r.label }));

function CreateUser() {
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [role, setRole] = useState("user");
  const [password, setPassword] = useState("");
  const roles = useRoleOptions();
  const t = useT("panel");
  const errorText = useErrorText();
  const create = useMutation({
    mutationFn: () => apiFetch<User>("/api/admin/users", {
      method: "POST", json: { username, role, ...(password ? { password } : {}) },
    }),
    onSuccess: (u) => {
      notifications.show({ message: t("users.created", { username: u.username }) });
      setUsername(""); setPassword("");
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
    onSettled: () => setPassword(""),
  });
  return (
    <FormSection title={t("users.add.title")} onSubmit={() => create.mutate()}
      error={create.error ? errorText(create.error, "users.create_failed") : null}>
      <TextField label={t("users.add.username")} name="new-username" required value={username} onChange={(e) => setUsername(e.target.value)} />
      <SelectField label={t("users.add.role")} name="new-role" options={roles} value={role} onChange={setRole} />
      <TextField label={t("users.add.password")} name="new-password" type="password" autoComplete="new-password"
        hint={t("users.add.password_hint")} value={password}
        onChange={(e) => setPassword(e.target.value)} />
      <div className="ag-row"><Button type="submit" variant="primary" disabled={create.isPending}>{t("users.add.submit")}</Button></div>
    </FormSection>
  );
}

function UserTable() {
  const me = useSession();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: ({ signal }) => apiFetch<User[]>("/api/admin/users", { signal }) });
  const roles = useRoleOptions();
  const [deactivate, setDeactivate] = useState<User | null>(null);
  const t = useT("panel");
  const errorText = useErrorText();
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: Partial<Pick<User, "role" | "is_active">> }) =>
      apiFetch<User>(`/api/admin/users/${id}`, { method: "PATCH", json: patch }),
    onSuccess: (u) => {
      notifications.show({ message: t("users.updated", { username: u.username }) });
      setDeactivate(null);
      void qc.invalidateQueries({ queryKey: ["users"] });
      void qc.invalidateQueries({ queryKey: ["roles"] });
    },
    onError: (e) => notifications.show({ color: "red", message: errorText(e, "users.update_failed") }),
  });

  // The UsersRolesTable pattern from ui.mantine.dev (frontend/lookbook/UsersRolesTable): the role is
  // changed in its cell.
  const columns: Column<User>[] = [
    { id: "username", header: t("users.column.user"), sortValue: (u) => u.username, cell: (u) => (
      <Group gap="sm" wrap="nowrap">
        <Avatar name={u.username} color="initials" radius="xl" aria-hidden />
        <div>
          <Text fz="sm" fw={500}>{u.username}</Text>
          {u.id === me?.id && <Text fz="xs" c="dimmed">{t("users.you")}</Text>}
        </div>
      </Group>
    ) },
    { id: "role", header: t("users.column.role"), sortValue: (u) => u.role, cell: (u) => (
      <Select data={roles} value={u.role} variant="unstyled" allowDeselect={false} aria-label={t("users.role_of", { username: u.username })}
        disabled={update.isPending} w={120}
        onChange={(role) => role && role !== u.role && update.mutate({ id: u.id, patch: { role } })} />
    ) },
    { id: "active", header: t("users.column.status"), sortValue: (u) => (u.is_active ? 1 : 0),
      cell: (u) => <StatusBadge tone={u.is_active ? "info" : "neutral"}>{u.is_active ? t("users.status.active") : t("users.status.inactive")}</StatusBadge> },
    { id: "actions", header: t("users.column.actions"), cell: (u) => u.is_active
      ? <IconAction danger label={t("users.deactivate")} icon={<UserX size={16} />} onClick={() => setDeactivate(u)} />
      : <IconAction label={t("users.activate")} icon={<UserCheck size={16} />} disabled={update.isPending} onClick={() => update.mutate({ id: u.id, patch: { is_active: true } })} /> },
  ];

  return (
    <>
      <DataTable caption={t("users.title")} columns={columns} rows={users.data} rowKey={(u) => String(u.id)}
        loading={users.isPending} error={users.error ? errorText(users.error, "users.load_failed") : null} onRetry={() => void users.refetch()}
        emptyTitle={t("users.empty")} />
      <ConfirmDialog opened={deactivate !== null} title={t("users.deactivate_confirm.title")} danger confirmLabel={t("users.deactivate")}
        busy={update.isPending} onCancel={() => setDeactivate(null)}
        onConfirm={() => deactivate && update.mutate({ id: deactivate.id, patch: { is_active: false } })}>
        <p>{t("users.deactivate_confirm.text", { username: deactivate?.username ?? "" })}</p>
      </ConfirmDialog>
    </>
  );
}

export function Users() {
  const navigate = useNavigate();
  const tab = useLocation().pathname.startsWith("/users/roles") ? "roles" : "users";
  const users = useCan("admin.users"), roles = useCan("admin.roles");
  const t = useT("panel");
  if (tab === "users" && !users && roles) return <Navigate to="/users/roles" replace />;
  return (
    <div className="ag-stack">
      <PageHeader title={t("users.title")} description={t("users.description")} />
      <Tabs value={tab} onChange={(next) => void navigate(next === "roles" ? "/users/roles" : "/users")}>
        <Tabs.List>
          {users && <Tabs.Tab value="users">{t("users.tab.users")}</Tabs.Tab>}
          {roles && <Tabs.Tab value="roles">{t("users.tab.roles")}</Tabs.Tab>}
        </Tabs.List>
        <Tabs.Panel value="users" pt="md"><div className="ag-stack"><UserTable /><CreateUser /></div></Tabs.Panel>
        <Tabs.Panel value="roles" pt="md"><Roles /></Tabs.Panel>
      </Tabs>
    </div>
  );
}
