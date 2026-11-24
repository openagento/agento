// Tools → Catalog (admin role only, PRD §4): every tool the enabled modules declare, with its
// labels and review status. The minute catalog sync adds tools and sends a changed contract back to
// review, so the list polls (UI-8).
import { useEffect, useMemo, useRef, useState } from "react";
import { Search, Tags } from "lucide-react";
import { Alert, Group, MultiSelect, SegmentedControl, Select, Stack, Text, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useQuery } from "@agento/api";
import { Button, CodeBlock, DataTable, EmptyState, IconAction, LabelBadge, labelIcon, ReviewState, type Column } from "@agento/ui";
import { TIGHT, wrapAnywhere, wrapPill } from "../layout";
import { message } from "./shared";
import { moduleOf, ToolClassifyDrawer, type CatalogRow, type Saved } from "./ToolClassifyDrawer";
import { CATALOG_KEY, CATALOG_POLL_MS, useToolLabels } from "./ToolLabels";
import type { Toolsets } from "./Tools";

const SYNC_COMMAND = "bin/agento tool:sync-catalog";
type Status = "all" | "to_review" | "reviewed";
/** A review run (Start review): its own record of what it showed and saved as reviewed, so its end
 *  message never reads a catalog refetch that has not come back yet. */
/** `saved`: tool id → the revision a save in this run marked reviewed. A later sync that moves the
 *  revision past it (a changed contract) puts the tool back in the count. */
interface Queue { visited: ReadonlySet<number>; saved: ReadonlyMap<number, number> }
const toolCount = (n: number) => (n === 1 ? "1 tool" : `${n} tools`);

export function ToolCatalog() {
  const list = useQuery({
    queryKey: CATALOG_KEY, refetchInterval: CATALOG_POLL_MS,
    queryFn: ({ signal }) => apiFetch<Toolsets>("/api/admin/tools?scope=default&scope_id=0", { signal }),
  });
  const labels = useToolLabels({ poll: true });
  const names = useMemo(() => new Map((labels.data ?? []).map((l) => [l.key, l.name])), [labels.data]);
  const [search, setSearch] = useState("");
  const [source, setSource] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>("all");
  const [wanted, setWanted] = useState<string[]>([]);
  // The row as it was when the drawer opened: the form starts from it, whatever a refetch brings.
  const [open, setOpen] = useState<CatalogRow | null>(null);
  const [queue, setQueue] = useState<Queue | null>(null);
  // A new key remounts the table, so a header sort goes back to catalog order, the queue's order.
  const [tableKey, setTableKey] = useState(0);

  const all = useMemo(() => (list.data ?? []).flatMap((t) => t.tools.map((i) => ({ ...i, toolset: t.toolset }))), [list.data]);
  const rows = useMemo(() => all.filter((i): i is typeof i & CatalogRow => Boolean(i.catalog)), [all]);
  const unsynced = all.length - rows.length;
  const sources = [...new Set(rows.map((r) => moduleOf(r.catalog.source)))].sort();
  const toReview = rows.filter((r) => !r.catalog.reviewed).length;
  const needle = search.trim().toLowerCase();
  const shown = rows.filter((r) => (!needle || r.name.toLowerCase().includes(needle))
    && (!source || moduleOf(r.catalog.source) === source)
    && (status === "all" || r.catalog.reviewed === (status === "reviewed"))
    && wanted.every((k) => r.catalog.labels.includes(k)));
  const filtered = Boolean(needle || source || status !== "all" || wanted.length);
  const clear = () => { setSearch(""); setSource(null); setStatus("all"); setWanted([]); };

  const unreviewed = rows.filter((r) => !r.catalog.reviewed);
  const isSaved = (q: Queue, r: CatalogRow) => (q.saved.get(r.catalog.id) ?? -1) >= r.catalog.revision;
  const leftIn = (q: Queue) => unreviewed.filter((r) => !isSaved(q, r)).length;
  const startReview = () => {
    const first = unreviewed[0];
    if (!first) return;
    clear();
    setStatus("to_review");
    setTableKey((k) => k + 1);
    setQueue({ visited: new Set([first.catalog.id]), saved: new Map() });
    setOpen(first);
  };
  const close = () => { setOpen(null); setQueue(null); };
  // The drawer reports a save only while its form is open; the run reads its newest state here.
  const current = useRef({ queue, open, unreviewed });
  useEffect(() => { current.current = { queue, open, unreviewed }; });
  /** After a save (the server's answer) or a skip (`null`): the next tool, or the end. */
  const advance = (id: number, saved: Saved | null) => {
    const { queue: q, open: shown, unreviewed: todo } = current.current;
    if (!q || shown?.catalog.id !== id) return;
    const marks = saved?.reviewed ? new Map([...q.saved, [id, saved.revision]]) : q.saved;
    const after = { ...q, saved: marks };
    const next = todo.find((r) => !q.visited.has(r.catalog.id) && !isSaved(after, r));
    if (next) {
      setQueue({ ...after, visited: new Set([...q.visited, next.catalog.id]) });
      setOpen(next);
      return;
    }
    const left = todo.filter((r) => !isSaved(after, r)).length;
    close();
    notifications.show({ message: left ? `Review paused. ${toolCount(left)} to review.` : "All tools are reviewed." });
  };

  // The review state, the module and the labels sit in the Tool cell, so a phone shows the whole
  // row with its Classify action (no side scroll). The status filter sorts out the review state.
  const columns: Column<CatalogRow>[] = [
    { id: "tool", header: "Tool", sortValue: (r) => r.name, cell: (r) => (
      <Stack gap={TIGHT}>
        <Group justify="space-between" gap="xs">
          <Text fz="sm" fw={500}>{r.name}</Text>
          <ReviewState reviewed={r.catalog.reviewed} />
        </Group>
        <Text fz="xs" c="dimmed">{moduleOf(r.catalog.source)}{r.catalog.labels.length ? "" : " · No labels"}</Text>
        {r.catalog.labels.length > 0 && (
          <Group gap={TIGHT}>{r.catalog.labels.map((k) => <LabelBadge key={k} labelKey={k} name={names.get(k) ?? k} />)}</Group>
        )}
      </Stack>
    ) },
    { id: "actions", header: "Actions", cell: (r) => (
      <IconAction label="Classify" icon={<Tags size={16} />} onClick={() => setOpen(r)} />
    ) },
  ];

  if (list.isSuccess && all.length > 0 && rows.length === 0) {
    return (
      <EmptyState title="The catalog is empty">
        <Stack gap="xs" align="center">
          <Text size="sm">The catalog fills on its next sync, within a minute. To fill it now, run:</Text>
          <CodeBlock code={SYNC_COMMAND} copy />
        </Stack>
      </EmptyState>
    );
  }
  return (
    <Stack gap="md">
      {list.isSuccess && toReview > 0 && (
        <Alert color="blue" variant="light" title={`${toolCount(toReview)} to review`}>
          <Group justify="space-between" gap="sm">
            <Text size="sm">Check each tool's labels. The trifecta warning is only as good as the labels.</Text>
            <Button variant="primary" onClick={startReview}>Start review</Button>
          </Group>
        </Alert>
      )}
      <Group gap="sm" align="flex-end">
        <TextInput aria-label="Search tools" placeholder="Search tools" w={240} leftSection={<Search size={16} aria-hidden />}
          value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
        <Select aria-label="Module" placeholder="All modules" data={sources} value={source} onChange={setSource} clearable
          searchable w={180} />
        <SegmentedControl aria-label="Status" value={status} onChange={(v) => setStatus(v as Status)}
          data={[{ value: "all", label: "All" }, { value: "to_review", label: `To review · ${toReview}` },
            { value: "reviewed", label: `Reviewed · ${rows.length - toReview}` }]} />
        <MultiSelect aria-label="Labels" placeholder={wanted.length ? undefined : "With all of these labels"} miw={240}
          data={(labels.data ?? []).map((l) => ({ value: l.key, label: l.name }))} value={wanted} onChange={setWanted}
          clearable searchable classNames={{ pill: wrapPill }} renderOption={({ option }) => (
            <Group gap="xs" wrap="nowrap">{labelIcon(option.value)}<Text size="sm" className={wrapAnywhere}>{option.label}</Text></Group>
          )} />
      </Group>
      <DataTable key={tableKey} caption="Tool catalog" columns={columns} rows={list.data ? shown : undefined} rowKey={(r) => String(r.catalog.id)}
        onRowActivate={setOpen}
        loading={list.isPending} error={list.error ? message(list.error) : null} onRetry={() => void list.refetch()}
        emptyTitle={filtered ? "No matching tools" : "No tools"}
        emptyText={filtered ? "No tool matches every filter." : "No enabled module declares a tool."}
        emptyAction={filtered ? <Button onClick={clear}>Clear filters</Button> : undefined} />
      {unsynced > 0 && (
        <Text size="sm" c="dimmed">
          {unsynced === 1 ? "1 tool is" : `${unsynced} tools are`} not in the catalog yet. The next sync, within a minute, adds them.
        </Text>
      )}
      <ToolClassifyDrawer opened={open} live={open ? rows.find((r) => r.catalog.id === open.catalog.id) : undefined}
        labels={labels.data} labelsError={labels.error ? message(labels.error) : null} onRetryLabels={() => void labels.refetch()}
        queue={queue && { left: leftIn(queue), onNext: (saved) => advance(saved.id, saved), onSkip: (id) => advance(id, null) }} onClose={close} />
    </Stack>
  );
}
