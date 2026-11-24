// Tools → Labels (admin role only, PRD §4): the label definitions. A built-in label is never renamed
// or deleted; a custom one keeps its key when renamed, because `agento run --stub-tools <key>` uses it.
import { useState } from "react";
import { Lock, Pencil, Trash2 } from "lucide-react";
import { Group, Modal, Stack, Text, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, ApiError, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, ConfirmDialog, DataTable, IconAction, LabelBadge, type Column } from "@agento/ui";
import { TIGHT, wrapAnywhere } from "../layout";
import { message, whole } from "./shared";

export interface ToolLabel { id: number; key: string; name: string; protected: boolean; usage: number }

export const LABELS_KEY = ["admin-tool-labels"];
export const CATALOG_KEY = ["admin-tool-catalog"];
/** The catalog sync (cron, every minute) adds tools and moves usage counts (UI-8). */
export const CATALOG_POLL_MS = 60_000;
const NAME_MAX = 100;

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

const tools = (n: number) => `${n} ${n === 1 ? "tool" : "tools"}`;

function NameDialog({ label, onClose }: { label: ToolLabel | "new" | null; onClose: () => void }) {
  const refresh = useRefreshTools();
  const editing = label !== "new" && label !== null ? label : null;
  const [name, setName] = useState(editing?.name ?? "");
  const save = useMutation({
    mutationFn: () => apiFetch<ToolLabel>(editing ? `/api/admin/tool-labels/${editing.id}` : "/api/admin/tool-labels", {
      method: editing ? "PATCH" : "POST", json: { name: name.trim() },
    }),
    onSuccess: (l) => {
      notifications.show({ message: editing ? `Label renamed to ${l.name}.` : `Label ${l.name} added. Its key is ${l.key}.` });
      refresh();
      onClose();
    },
  });
  const trimmed = name.trim();
  const unchanged = editing !== null && trimmed === editing.name;
  return (
    <Modal opened={label !== null} onClose={onClose} title={editing ? "Rename label" : "Add label"} centered
      closeButtonProps={{ "aria-label": "Close" }}>
      <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <Stack gap="md">
          <TextInput label="Name" placeholder="Finance" required data-autofocus value={name} maxLength={NAME_MAX}
            onChange={(e) => setName(e.currentTarget.value)}
            description={editing ? `The key stays ${editing.key}.` : "The key is made from the name and cannot change later."}
            error={save.error instanceof ApiError && save.error.status === 409 ? "A label with this name already exists."
              : save.error ? message(save.error) : null} />
          <Group justify="flex-end">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={!trimmed || unchanged || save.isPending}>
              {editing ? "Rename" : "Add label"}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

export function ToolLabels() {
  const labels = useToolLabels({ poll: true });
  const refresh = useRefreshTools();
  const [naming, setNaming] = useState<ToolLabel | "new" | null>(null);
  const [deleting, setDeleting] = useState<ToolLabel | null>(null);
  const remove = useMutation({
    mutationFn: (l: ToolLabel) => apiFetch(`/api/admin/tool-labels/${l.id}`, { method: "DELETE" }),
    onSuccess: (_r, l) => { notifications.show({ message: `Label ${l.name} deleted.` }); setDeleting(null); },
    onError: (e) => notifications.show({ color: "red", message: message(e) }),
    onSettled: refresh,
  });

  const columns: Column<ToolLabel>[] = [
    { id: "label", header: "Label", sortValue: (l) => l.name, cell: (l) => (
      <Stack gap={TIGHT} align="flex-start">
        <LabelBadge labelKey={l.key} name={l.name} />
        <Text fz="xs" c="dimmed" ff="monospace" className={wrapAnywhere}>{l.key}</Text>
      </Stack>
    ) },
    { id: "usage", header: "Tools", sortValue: (l) => l.usage, cell: (l) => l.usage
      ? <Text fz="sm">{tools(l.usage)}</Text> : <Text fz="sm" c="dimmed">Not used</Text> },
    // A built-in label has no action; the cell says why instead of staying blank.
    { id: "actions", header: "Actions", cell: (l) => l.protected ? whole(
      <Group gap="xs" wrap="nowrap">
        <Lock size={14} aria-hidden />
        <Text fz="sm" c="dimmed">Built-in</Text>
      </Group>
    ) : (
      <Group gap="xs" wrap="nowrap">
        <IconAction label="Rename" icon={<Pencil size={16} />} onClick={() => setNaming(l)} />
        <IconAction danger label="Delete" icon={<Trash2 size={16} />} onClick={() => setDeleting(l)} />
      </Group>
    ) },
  ];

  return (
    <Stack gap="md">
      {/* Wraps: on a phone the button goes under the text instead of past the screen edge. */}
      <Group justify="space-between" align="flex-start">
        <Text size="sm" c="dimmed" flex="1 1 20rem">
          A label groups tools. Use its key with <Text span ff="monospace" fz="sm">agento run --stub-tools</Text>.
          Built-in labels cannot be renamed or deleted.
        </Text>
        <Button variant="primary" onClick={() => setNaming("new")}>Add label</Button>
      </Group>
      <DataTable caption="Tool labels" columns={columns} rows={labels.data} rowKey={(l) => String(l.id)}
        onRowActivate={(l) => !l.protected && setNaming(l)}
        loading={labels.isPending} error={labels.error ? message(labels.error) : null} onRetry={() => void labels.refetch()}
        emptyTitle="No labels" />
      {/* Keyed: each open starts from the label's current name. */}
      <NameDialog key={naming === null ? "closed" : naming === "new" ? "new" : naming.id} label={naming}
        onClose={() => setNaming(null)} />
      <ConfirmDialog opened={deleting !== null} title="Delete this label?" danger confirmLabel="Delete"
        busy={remove.isPending} onCancel={() => setDeleting(null)} onConfirm={() => deleting && remove.mutate(deleting)}>
        <p>
          {deleting?.usage ? `${deleting.name} is removed from ${tools(deleting.usage)}.` : `No tool has ${deleting?.name}.`}{" "}
          A run with <code>--stub-tools {deleting?.key}</code> then stops before it starts.
        </p>
      </ConfirmDialog>
    </Stack>
  );
}
