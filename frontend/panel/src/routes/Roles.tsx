// Roles (plan "Panel roles"): a role is a row; its access is edited on the role page as a tree.
import { useState } from "react";
import { useNavigate } from "react-router";
import { Pencil, Trash2 } from "lucide-react";
import { Badge, Group, Modal, Stack, Text, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useErrorText, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, ConfirmDialog, DataTable, IconAction, useFormat, useT, type Column } from "@agento/ui";
import { whole } from "./admin/shared";

export interface Role { code: string; label: string; builtin: boolean; users: number; scopes: number }

export const ROLE_CODE = /^[a-z][a-z0-9_]{1,15}$/;

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
  const t = useT("panel");
  const errorText = useErrorText();
  const create = useMutation({
    mutationFn: () => apiFetch<Role>("/api/admin/roles", { method: "POST", json: { code: value, label: label.trim() } }),
    onSuccess: (r) => {
      notifications.show({ message: t("roles.created", { label: r.label }) });
      void qc.invalidateQueries({ queryKey: ["roles"] });
      void navigate(`/users/roles/${r.code}?tab=resources`);
    },
  });
  const close = () => { setLabel(""); setCode(null); create.reset(); onClose(); };
  return (
    <Modal opened={opened} onClose={close} title={t("roles.add")} centered closeButtonProps={{ "aria-label": t("action.close") }}>
      <form onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <Stack gap="md">
          <TextInput label={t("roles.name")} placeholder={t("roles.name_placeholder")} required data-autofocus value={label}
            onChange={(e) => setLabel(e.currentTarget.value)} />
          <TextInput label={t("roles.code")} required value={value} onChange={(e) => setCode(e.currentTarget.value)}
            description={t("roles.code_hint")}
            error={value && !ROLE_CODE.test(value) ? t("roles.code_invalid")
              : create.error ? errorText(create.error, "roles.create_failed") : null} />
          <Group justify="flex-end">
            <Button onClick={close}>{t("action.cancel")}</Button>
            <Button type="submit" variant="primary" disabled={!label.trim() || !ROLE_CODE.test(value) || create.isPending}>
              {t("roles.add")}
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
  const t = useT("panel");
  const { number } = useFormat();
  const errorText = useErrorText();
  const remove = useMutation({
    mutationFn: (code: string) => apiFetch(`/api/admin/roles/${code}`, { method: "DELETE" }),
    onSuccess: () => { notifications.show({ message: t("roles.deleted", { label: deleting?.label ?? "" }) }); setDeleting(null); },
    onError: (e) => notifications.show({ color: "red", message: errorText(e, "roles.delete_failed") }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["roles"] }),
  });
  const edit = (r: Role) => void navigate(`/users/roles/${r.code}`);
  const whyKept = (r: Role) => r.builtin ? t("roles.kept.builtin")
    : r.users ? t("roles.kept.users", { count: r.users }) : undefined;

  const columns: Column<Role>[] = [
    { id: "role", header: t("roles.column.role"), sortValue: (r) => r.label, cell: (r) => (
      <Group gap="sm" wrap="nowrap">
        <div>
          <Text fz="sm" fw={500}>{r.label}</Text>
          <Text fz="xs" c="dimmed" ff="monospace">{r.code}</Text>
        </div>
        {r.builtin && whole(<Badge variant="light" color="gray" size="sm">{t("roles.builtin")}</Badge>)}
      </Group>
    ) },
    { id: "users", header: t("roles.column.users"), sortValue: (r) => r.users, cell: (r) => <Text fz="sm">{number(r.users)}</Text> },
    { id: "access", header: t("roles.column.access"), sortValue: (r) => r.scopes, cell: (r) => r.scopes
      ? <Text fz="sm">{t("roles.places", { count: r.scopes })}</Text> : <Text fz="sm" c="dimmed">{t("roles.no_access")}</Text> },
    { id: "actions", header: t("roles.column.actions"), cell: (r) => (
      <Group gap="xs" wrap="nowrap">
        <IconAction label={t("action.edit")} icon={<Pencil size={16} />} onClick={() => edit(r)} />
        <IconAction danger label={t("roles.delete")} icon={<Trash2 size={16} />} disabled={Boolean(whyKept(r))} title={whyKept(r)}
          onClick={() => setDeleting(r)} />
      </Group>
    ) },
  ];

  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Text size="sm" c="dimmed">{t("roles.hint")}</Text>
        <Button variant="primary" onClick={() => setAdding(true)}>{t("roles.add")}</Button>
      </Group>
      <DataTable caption={t("roles.title")} columns={columns} rows={roles.data} rowKey={(r) => r.code} onRowActivate={edit}
        loading={roles.isPending} error={roles.error ? errorText(roles.error, "roles.load_failed") : null} onRetry={() => void roles.refetch()}
        emptyTitle={t("roles.empty")} />
      <AddRole opened={adding} onClose={() => setAdding(false)} />
      <ConfirmDialog opened={deleting !== null} title={t("roles.delete_confirm.title")} danger confirmLabel={t("roles.delete")}
        busy={remove.isPending} onCancel={() => setDeleting(null)} onConfirm={() => deleting && remove.mutate(deleting.code)}>
        <p>{t("roles.delete_confirm.text", { label: deleting?.label ?? "" })}</p>
      </ConfirmDialog>
    </Stack>
  );
}
