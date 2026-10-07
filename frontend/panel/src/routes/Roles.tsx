// Roles (plan "Panel roles"): a role is a row; its access is edited on the role page as a tree.
import { useState } from "react";
import { useNavigate } from "react-router";
import { Pencil, Trash2 } from "lucide-react";
import { Badge, Group, Modal, Stack, Text, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, ConfirmDialog, DataTable, IconAction, type Column } from "@agento/ui";
import { message, whole } from "./admin/shared";

export interface Role { code: string; label: string; builtin: boolean; users: number; scopes: number }

export const ROLE_CODE = /^[a-z][a-z0-9_]{1,15}$/;
const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

export const useRoles = () => useQuery({
  queryKey: ["roles"], queryFn: ({ signal }) => apiFetch<Role[]>("/api/admin/roles", { signal }),
});

/** "Support agent" → "support_agent": a code the API accepts, or as close as the label allows. */
const codeOf = (label: string) => label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^[^a-z]+|_+$/g, "").slice(0, 16);

function AddRole({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [label, setLabel] = useState("");
  const [code, setCode] = useState<string | null>(null); // null: follows the label
  const value = code ?? codeOf(label);
  const create = useMutation({
    mutationFn: () => apiFetch<Role>("/api/admin/roles", { method: "POST", json: { code: value, label: label.trim() } }),
    onSuccess: (r) => {
      notifications.show({ message: `Role ${r.label} created. Choose what it may use.` });
      void qc.invalidateQueries({ queryKey: ["roles"] });
      void navigate(`/users/roles/${r.code}?tab=resources`);
    },
  });
  const close = () => { setLabel(""); setCode(null); create.reset(); onClose(); };
  return (
    <Modal opened={opened} onClose={close} title="Add role" centered closeButtonProps={{ "aria-label": "Close" }}>
      <form onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <Stack gap="md">
          <TextInput label="Name" placeholder="Support agent" required data-autofocus value={label}
            onChange={(e) => setLabel(e.currentTarget.value)} />
          <TextInput label="Code" required value={value} onChange={(e) => setCode(e.currentTarget.value)}
            description="Lowercase letters, digits and _, starting with a letter; 2 to 16 characters. It cannot change later."
            error={value && !ROLE_CODE.test(value) ? "Not a valid code." : create.error ? message(create.error) : null} />
          <Group justify="flex-end">
            <Button onClick={close}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!label.trim() || !ROLE_CODE.test(value) || create.isPending}>
              Add role
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

export function Roles() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const roles = useRoles();
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<Role | null>(null);
  const remove = useMutation({
    mutationFn: (code: string) => apiFetch(`/api/admin/roles/${code}`, { method: "DELETE" }),
    onSuccess: () => { notifications.show({ message: `Role ${deleting?.label} deleted.` }); setDeleting(null); },
    onError: (e) => notifications.show({ color: "red", message: message(e) }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["roles"] }),
  });
  const edit = (r: Role) => void navigate(`/users/roles/${r.code}`);
  const whyKept = (r: Role) => r.builtin ? "A built-in role cannot be deleted."
    : r.users ? `${plural(r.users, "user")} still ${r.users === 1 ? "has" : "have"} this role.` : undefined;

  const columns: Column<Role>[] = [
    { id: "role", header: "Role", sortValue: (r) => r.label, cell: (r) => (
      <Group gap="sm" wrap="nowrap">
        <div>
          <Text fz="sm" fw={500}>{r.label}</Text>
          <Text fz="xs" c="dimmed" ff="monospace">{r.code}</Text>
        </div>
        {r.builtin && whole(<Badge variant="light" color="gray" size="sm">Built-in</Badge>)}
      </Group>
    ) },
    { id: "users", header: "Users", sortValue: (r) => r.users, cell: (r) => <Text fz="sm">{r.users}</Text> },
    { id: "access", header: "Access", sortValue: (r) => r.scopes, cell: (r) => r.scopes
      ? <Text fz="sm">{plural(r.scopes, "place")}</Text> : <Text fz="sm" c="dimmed">No access yet</Text> },
    { id: "actions", header: "Actions", cell: (r) => (
      <Group gap="xs" wrap="nowrap">
        <IconAction label="Edit" icon={<Pencil size={16} />} onClick={() => edit(r)} />
        <IconAction danger label="Delete" icon={<Trash2 size={16} />} disabled={Boolean(whyKept(r))} title={whyKept(r)}
          onClick={() => setDeleting(r)} />
      </Group>
    ) },
  ];

  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Text size="sm" c="dimmed">A role may use only what it is given, and only where it is given.</Text>
        <Button variant="primary" onClick={() => setAdding(true)}>Add role</Button>
      </Group>
      <DataTable caption="Roles" columns={columns} rows={roles.data} rowKey={(r) => r.code} onRowActivate={edit}
        loading={roles.isPending} error={roles.error ? message(roles.error) : null} onRetry={() => void roles.refetch()}
        emptyTitle="No roles" />
      <AddRole opened={adding} onClose={() => setAdding(false)} />
      <ConfirmDialog opened={deleting !== null} title="Delete this role?" danger confirmLabel="Delete"
        busy={remove.isPending} onCancel={() => setDeleting(null)} onConfirm={() => deleting && remove.mutate(deleting.code)}>
        <p>{deleting?.label} and everything it was given are removed.</p>
      </ConfirmDialog>
    </Stack>
  );
}
