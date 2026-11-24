// One tool's labels and review status. The form keeps its own state across the catalog poll; when
// the row's `revision` moves under it, it says so and still saves with the revision it was opened
// on, so the server answers 409 rather than overwrite a change the admin never saw.
import { useEffect, useRef, useState } from "react";
import { Alert, Group, SegmentedControl, Stack, TagsInput, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, ApiError, useMutation } from "@agento/api";
import { Button, ErrorState, labelIcon, LoadingState, ReviewState, RISK_NAMES, SideDrawer } from "@agento/ui";
import type { CatalogEntry } from "../enablementTree";
import { wrapAnywhere, wrapPill } from "../layout";
import { message } from "./shared";
import { useRefreshTools, type ToolLabel } from "./ToolLabels";

export interface CatalogRow { name: string; toolset: string; catalog: CatalogEntry }
export const moduleOf = (source: string) => source.replace(/^module:/, "");

type Review = "reviewed" | "to_review";
/** What a save in a review run reports: the server's answer for that tool, not the form's state now. */
export interface Saved { id: number; reviewed: boolean; revision: number }
/** A review run (Start review): `left` counts the tools still to review, this one included. */
export interface ReviewQueue { left: number; onNext: (saved: Saved) => void; onSkip: (id: number) => void }

/** The server's normalized label name (NFKC, case-folded, trimmed, inner spaces collapsed). */
const norm = (s: string) => s.normalize("NFKC").toLowerCase().trim().replace(/\s+/g, " ");

/** A selected label: an existing one by key (a rename elsewhere does not change it), or a new name. */
type Chip = { key: string } | { name: string };

/** Typed or picked text → an existing label (by name, else by key: a renamed label keeps its key,
 *  and a new label may take that key's old name), or a new label's name. */
function chipOf(text: string, labels: ToolLabel[]): Chip {
  const hit = labels.find((l) => norm(l.name) === norm(text)) ?? labels.find((l) => l.key === norm(text));
  return hit ? { key: hit.key } : { name: text.trim() };
}

/** The chips → the keys of existing labels and the names of new ones, each once. */
function split(chips: Chip[]) {
  const keys = new Set<string>();
  const fresh = new Map<string, string>();
  for (const chip of chips) {
    if ("key" in chip) keys.add(chip.key);
    else if (norm(chip.name)) fresh.set(norm(chip.name), chip.name);
  }
  return { keys: [...keys], fresh: [...fresh.values()] };
}

class Conflict extends Error {}

function ClassifyForm({ opened, live, labels, queue, onDone }: {
  opened: CatalogRow; live: CatalogRow | undefined; labels: ToolLabel[]; queue: ReviewQueue | null; onDone: () => void;
}) {
  const refresh = useRefreshTools();
  const nameOf = (key: string) => labels.find((l) => l.key === key)?.name ?? key;
  const [base, setBase] = useState(opened);
  const reviewOf = (row: CatalogRow): Review => (row.catalog.reviewed ? "reviewed" : "to_review");
  const [chips, setChips] = useState<Chip[]>(() => base.catalog.labels.map((key) => ({ key })));
  const shown = chips.map((c) => ("key" in c ? nameOf(c.key) : c.name));
  // An unchanged value keeps its chip (its key); only new text is looked up. One chip per label.
  const change = (values: string[]) => {
    const next = values.map((v) => (shown.includes(v) ? chips[shown.indexOf(v)] : chipOf(v, labels)));
    const id = (c: Chip) => ("key" in c ? `k:${c.key}` : `n:${norm(c.name)}`);
    setChips(next.filter((c, i) => next.findIndex((o) => id(o) === id(c)) === i));
  };
  // A review run is there to mark tools reviewed: it starts at Reviewed, and the admin may still say no.
  const [review, setReview] = useState<Review>(() => (queue ? "reviewed" : reviewOf(base)));
  const reload = (row: CatalogRow) => { setBase(row); setChips(row.catalog.labels.map((key) => ({ key }))); setReview(reviewOf(row)); };

  const { keys, fresh } = split(chips);
  const reviewChange = review === reviewOf(base) ? null : review;
  const dirty = fresh.length > 0 || reviewChange !== null
    || [...keys].sort().join() !== [...base.catalog.labels].sort().join();
  const moved = live !== undefined && live.catalog.revision !== base.catalog.revision;
  // In a review run the next tool's form replaces the focused button: focus goes to the dialog,
  // whose name is the tool's name, so the keyboard stays in the drawer and the dropdown stays shut.
  const root = useRef<HTMLDivElement>(null);
  const inRun = queue !== null;
  useEffect(() => { if (inRun) root.current?.closest<HTMLElement>("[role=dialog]")?.focus(); }, [inRun]);

  const save = useMutation({
    // One new label rides in the classification itself (atomic); any others are created first.
    mutationFn: async (sent: { review: Review | null }) => {
      const created = [];
      for (const name of fresh.slice(1)) {
        created.push((await apiFetch<ToolLabel>("/api/admin/tool-labels", { method: "POST", json: { name } })).key);
      }
      try {
        return await apiFetch<{ reviewed: boolean; revision: number }>(`/api/admin/tools/${base.catalog.id}/classification`, {
          method: "PATCH",
          json: { revision: base.catalog.revision, labels: [...keys, ...created], new_label: fresh[0] ?? null, review: sent.review },
        });
      } catch (e) {
        throw e instanceof ApiError && e.status === 409 ? new Conflict() : e;
      }
    },
    onSettled: refresh,
  });

  // Given to mutate(), not to useMutation: they run only while this form is open, so a save that
  // answers after the drawer closed or moved on never moves, closes or advises the drawer shown now.
  // In a review run the next tool is the confirmation: one message at the end, not one per tool.
  const submit = () => save.mutate({ review: reviewChange }, {
    onSuccess: (saved) => {
      if (queue) return queue.onNext({ id: base.catalog.id, reviewed: saved.reviewed, revision: saved.revision });
      notifications.show({ message: `Saved ${base.name}.` });
      onDone();
    },
    onError: (e) => notifications.show({
      color: "red",
      message: e instanceof Conflict ? `${base.name} was changed since you opened it. Reload it, then save again.` : message(e),
    }),
  });

  return (
    <Stack gap="lg" ref={root}>
      <Group gap="sm">
        <Text size="sm" c="dimmed">Module {moduleOf(base.catalog.source)} · toolset {base.toolset}</Text>
        <ReviewState reviewed={base.catalog.reviewed} />
      </Group>
      {queue && <Text size="sm" fw={500}>{queue.left} left</Text>}
      {moved && (
        <Alert color="yellow" variant="light" title="Changed on the server — reload">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">This tool changed after you opened it. Reload to start from the new version; your edits here are dropped.</Text>
            <Button onClick={() => reload(live)}>Reload</Button>
          </Stack>
        </Alert>
      )}
      {live === undefined && (
        <Alert color="yellow" variant="light" title="No longer in the catalog">
          <Text size="sm">Its module was disabled or no longer declares it. Its labels are kept for when it comes back.</Text>
        </Alert>
      )}
      <Stack gap="xs">
        <Text size="sm" fw={500} id="review-label">Review</Text>
        <SegmentedControl aria-labelledby="review-label" value={review} onChange={(v) => setReview(v as Review)} disabled={save.isPending}
          data={[{ value: "to_review", label: "To review" }, { value: "reviewed", label: "Reviewed" }]} />
        <Text size="xs" c="dimmed">Reviewed means you checked the labels for this version of the tool. A change to the tool sends it back to review.</Text>
      </Stack>
      {/* The input comes last, with its hints above it: its open dropdown covers whatever is below,
          so the review decision sits above it too. No autofocus, so the dropdown does not open by itself.
          A label is added only by Enter or by picking an option (the hint's words): no comma split, no
          add on blur, and a paste goes into the text (Mantine's own paste handler would add it). */}
      <div onPasteCapture={(e) => e.stopPropagation()}>
        <TagsInput label="Labels" data={labels.map((l) => l.name)} value={shown} onChange={change} acceptValueOnBlur={false} splitChars={[]} clearable disabled={save.isPending} classNames={{ pill: wrapPill }}
          description={<>
            Pick a label, or type a new name and press Enter.{" "}
            {Object.values(RISK_NAMES).join(", ")}: an agent with all three can be made to leak private data.
            {(keys.length > 0 || fresh.length > 0) && <>
              <br />Keys for <Text span ff="monospace" inherit>--stub-tools</Text>:{" "}
              <Text span ff="monospace" inherit>{keys.join(", ")}</Text>
              {fresh.length > 0 && `${keys.length ? ", " : ""}${fresh.length === 1 ? "1 new label" : `${fresh.length} new labels`} (key on save)`}
            </>}
          </>}
          renderOption={({ option }) => {
            const key = labels.find((l) => l.name === option.value)?.key;
            return (
              <Group gap="xs" wrap="nowrap" w="100%">
                {key && labelIcon(key)}
                <Text size="sm" flex={1} className={wrapAnywhere}>{option.value}</Text>
                <Text size="xs" c="dimmed" ff="monospace" maw="40%" className={wrapAnywhere}>{key}</Text>
              </Group>
            );
          }} />
      </div>
      <Group justify="flex-end">
        {queue ? <Button onClick={() => queue.onSkip(base.catalog.id)} disabled={save.isPending}>Skip</Button> : <Button onClick={onDone}>Cancel</Button>}
        <Button variant="primary" onClick={submit} disabled={!dirty || save.isPending}>
          {queue ? "Save and next" : "Save"}
        </Button>
      </Group>
    </Stack>
  );
}

/** `opened`: the row when the drawer opened; `live`: the same tool in the latest catalog read.
 *  The form waits for the labels: without them a chip cannot be told from a new label. */
export function ToolClassifyDrawer({ opened, live, labels, labelsError, onRetryLabels, queue = null, onClose }: {
  opened: CatalogRow | null; live: CatalogRow | undefined; labels: ToolLabel[] | undefined;
  labelsError: string | null; onRetryLabels: () => void; queue?: ReviewQueue | null; onClose: () => void;
}) {
  const body = !opened ? null
    : labels ? <ClassifyForm key={opened.catalog.id} opened={opened} live={live} labels={labels} queue={queue} onDone={onClose} />
    : labelsError ? <ErrorState message={labelsError} onRetry={onRetryLabels} /> : <LoadingState />;
  return <SideDrawer opened={opened !== null} onClose={onClose} title={opened?.name ?? ""}>{body}</SideDrawer>;
}
