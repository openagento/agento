// Tools → Labels (admin role only, PRD §4): the label definitions. A built-in label is never renamed
// or deleted; a custom one keeps its key when renamed, because `agento run --stub-tools <key>` uses it.
import { useState } from "react";
import { Lock, Pencil, Trash2 } from "lucide-react";
import { Group, Modal, Stack, Text, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, ApiError, useErrorText, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, ConfirmDialog, DataTable, IconAction, LabelBadge, useT, withElements, type Column } from "@agento/ui";
import { TIGHT, wrapAnywhere } from "../layout";
import { whole } from "./shared";

export interface ToolLabel { id: number; key: string; name: string; protected: boolean; usage: number }

export const LABELS_KEY = ["admin-tool-labels"];
export const CATALOG_KEY = ["admin-tool-catalog"];
/** The catalog sync (cron, every minute) adds tools and moves usage counts (UI-8). */
export const CATALOG_POLL_MS = 60_000;
const NAME_MAX = 100;
/** Command text: the same in every language. */
const STUB_COMMAND = "agento run --stub-tools";
const STUB_FLAG = "--stub-tools";

/** `poll`: the screen shows what the sync moves (usage, the catalog); label names alone do not. */
export const useToolLabels = ({ enabled = true, poll = false } = {}) => useQuery({
  queryKey: LABELS_KEY, enabled, refetchInterval: poll ? CATALOG_POLL_MS : false,
  queryFn: ({ signal }) => apiFetch<ToolLabel[]>("/api/admin/tool-labels", { signal }),
});

/** After a label or classification write: the labels, the catalog and every scope's tool tree. */
export function useRefreshTools() {
  const qc = useQueryClient();
  return () => {
    for (const queryKey of [LABELS_KEY, CATALOG_KEY, ["admin-tools"]]) void qc.invalidateQueries({ queryKey });
  };
}

function NameDialog({ label, onClose }: { label: ToolLabel | "new" | null; onClose: () => void }) {
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const refresh = useRefreshTools();
  const editing = label !== "new" && label !== null ? label : null;
  const [name, setName] = useState(editing?.name ?? "");
  const save = useMutation({
    mutationFn: () => apiFetch<ToolLabel>(editing ? `/api/admin/tool-labels/${editing.id}` : "/api/admin/tool-labels", {
      method: editing ? "PATCH" : "POST", json: { name: name.trim() },
    }),
    onSuccess: (l) => {
      notifications.show({ message: editing ? t("labels.renamed", { name: l.name }) : t("labels.added", { name: l.name, key: l.key }) });
      refresh();
      onClose();
    },
  });
  const trimmed = name.trim();
  const unchanged = editing !== null && trimmed === editing.name;
  return (
    <Modal opened={label !== null} onClose={onClose} title={editing ? t("labels.rename.title") : t("labels.add")} centered
      closeButtonProps={{ "aria-label": t("action.close") }}>
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <Stack gap="md">
          <TextInput label={t("labels.name")} placeholder={t("labels.name_placeholder")} required data-autofocus value={name} maxLength={NAME_MAX}
            onChange={(e) => setName(e.currentTarget.value)}
            description={editing ? t("labels.key_stays", { key: editing.key }) : t("labels.key_made")}
            error={save.error instanceof ApiError && save.error.status === 409 ? t("labels.exists")
              : save.error ? errorText(save.error, "labels.save_failed") : null} />
          <Group justify="flex-end">
            <Button onClick={onClose}>{t("action.cancel")}</Button>
            <Button type="submit" variant="primary" disabled={!trimmed || unchanged || save.isPending}>
              {editing ? t("labels.rename") : t("labels.add")}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

export function ToolLabels() {
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const labels = useToolLabels({ poll: true });
  const refresh = useRefreshTools();
  const [naming, setNaming] = useState<ToolLabel | "new" | null>(null);
  const [deleting, setDeleting] = useState<ToolLabel | null>(null);
  const remove = useMutation({
    mutationFn: (l: ToolLabel) => apiFetch(`/api/admin/tool-labels/${l.id}`, { method: "DELETE" }),
    onSuccess: (_r, l) => { notifications.show({ message: t("labels.deleted", { name: l.name }) }); setDeleting(null); },
    onError: (e) => notifications.show({ color: "red", message: errorText(e, "labels.delete_failed") }),
    onSettled: refresh,
  });

  const columns: Column<ToolLabel>[] = [
    { id: "label", header: t("labels.column.label"), sortValue: (l) => l.name, cell: (l) => (
      <Stack gap={TIGHT} align="flex-start">
        <LabelBadge labelKey={l.key} name={l.name} />
        <Text fz="xs" c="dimmed" ff="monospace" className={wrapAnywhere}>{l.key}</Text>
      </Stack>
    ) },
    { id: "usage", header: t("labels.column.tools"), sortValue: (l) => l.usage, cell: (l) => l.usage
      ? <Text fz="sm">{t("labels.usage", { count: l.usage })}</Text> : <Text fz="sm" c="dimmed">{t("labels.not_used")}</Text> },
    // A built-in label has no action; the cell says why instead of staying blank.
    { id: "actions", header: t("labels.column.actions"), cell: (l) => l.protected ? whole(
      <Group gap="xs" wrap="nowrap">
        <Lock size={14} aria-hidden />
        <Text fz="sm" c="dimmed">{t("labels.builtin")}</Text>
      </Group>
    ) : (
      <Group gap="xs" wrap="nowrap">
        <IconAction label={t("labels.rename")} icon={<Pencil size={16} />} onClick={() => setNaming(l)} />
        <IconAction danger label={t("labels.delete")} icon={<Trash2 size={16} />} onClick={() => setDeleting(l)} />
      </Group>
    ) },
  ];

  return (
    <Stack gap="md">
      {/* Wraps: on a phone the button goes under the text instead of past the screen edge. */}
      <Group justify="space-between" align="flex-start">
        <Text size="sm" c="dimmed" flex="1 1 20rem">
          {withElements(t("labels.intro"), { command: <Text span ff="monospace" fz="sm">{STUB_COMMAND}</Text> })}
        </Text>
        <Button variant="primary" onClick={() => setNaming("new")}>{t("labels.add")}</Button>
      </Group>
      <DataTable caption={t("labels.caption")} columns={columns} rows={labels.data} rowKey={(l) => String(l.id)}
        onRowActivate={(l) => !l.protected && setNaming(l)}
        loading={labels.isPending} error={labels.error ? errorText(labels.error, "labels.load_failed") : null} onRetry={() => void labels.refetch()}
        emptyTitle={t("labels.empty")} />
      {/* Keyed: each open starts from the label's current name. */}
      <NameDialog key={naming === null ? "closed" : naming === "new" ? "new" : naming.id} label={naming}
        onClose={() => setNaming(null)} />
      <ConfirmDialog opened={deleting !== null} title={t("labels.delete.title")} danger confirmLabel={t("labels.delete")}
        busy={remove.isPending} onCancel={() => setDeleting(null)} onConfirm={() => deleting && remove.mutate(deleting)}>
        <p>
          {deleting?.usage ? t("labels.delete.used", { name: deleting.name, count: deleting.usage }) : t("labels.delete.unused", { name: deleting?.name ?? "" })}{" "}
          {withElements(t("labels.delete.effect"), { flag: <code>{`${STUB_FLAG} ${deleting?.key ?? ""}`}</code> })}
        </p>
      </ConfirmDialog>
    </Stack>
  );
}
