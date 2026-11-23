import { useEffect, useState } from "react";
import { Columns3, Eye, ListChecks, RotateCcw, Search } from "lucide-react";
import { Box, Checkbox, Group, Indicator, Loader, Popover, SegmentedControl, Stack, Text, TextInput } from "@mantine/core";
import { useLocalStorage } from "@mantine/hooks";
import { useNavigate, useSearchParams } from "react-router";
import { apiFetch, keepPreviousData, LIVE_REFETCH_MS, useJobLinks, useQuery } from "@agento/api";
import {
  Button, DataTable, durationOf, ErrorState, formatDuration, IconAction, JobDetail, jobTone, lastPage, LoadingState, OpenAction,
  PageHeader, Pager, SideDrawer, StatusBadge, Timestamp, useGridParams, type Column, type JobDetailData,
} from "@agento/ui";
import { message, whole, type JobHead, type JobRow } from "./shared";

const STATUSES = ["All", "TODO", "RUNNING", "SUCCESS", "FAILED", "DEAD"];
const DEFAULT_SORT = { id: "id", desc: true };
interface JobPage { rows: JobRow[]; total: number; page: number; page_size: number }
const when = (v: string | null | undefined) => (v ? <Timestamp value={v} /> : "—");

const seconds = (j: JobHead) => formatDuration(durationOf(j));

export const jobColumns = (open?: (j: JobHead) => void): Column<JobHead>[] => [
  { id: "id", header: "ID", cell: (j) => j.id, sortValue: (j) => j.id },
  { id: "type", header: "Type", cell: (j) => j.type, sortValue: (j) => j.type },
  { id: "status", header: "Status", cell: (j) => whole(<StatusBadge tone={jobTone(j.status)}>{j.status}</StatusBadge>), sortValue: (j) => j.status },
  { id: "view", header: "Agent view", cell: (j) => j.agent_view_code ?? "—", sortValue: (j) => j.agent_view_code ?? "" },
  { id: "ref", header: "Reference", cell: (j) => <Text size="sm" lineClamp={2} style={{ overflowWrap: "break-word" }}>{j.reference_id ?? "—"}</Text>, sortValue: (j) => j.reference_id ?? "" },
  { id: "created", header: "Created", cell: (j) => when(j.created_at), sortValue: (j) => j.created_at ?? "", nowrap: true },
  { id: "duration", header: "Duration", cell: seconds, sortValue: (j) => durationOf(j) ?? -1 },
  ...(open ? [{ id: "actions", header: "Actions", cell: (j: JobHead) => <IconAction label="Details" icon={<Eye size={16} />} onClick={() => open(j)} /> }] : []),
];

// The order of the columns, stated once: the table and the picker both read it. Actions is not
// here: it is always the last column, and the only mouse path to a job's detail.
const ORDER = ["id", "type", "status", "view", "source", "agent_type", "ref",
  "created", "started", "finished", "duration", "input_tokens", "output_tokens", "error_class"];
const DEFAULT_VISIBLE = ["id", "type", "status", "view", "ref", "created", "duration"];
// Per browser: a viewer's convenience, not shared state. Mantine's hook catches a storage that throws.
const STORAGE_KEY = "agento.admin.jobs.columns";

const text = (v: string | null) => v ?? "—";
const count = (v: number | null) => v ?? "—";
// The columns the API sends but the Dashboard does not have (JobRow fields only).
const EXTRA: Column<JobRow>[] = [
  { id: "source", header: "Source", cell: (j) => text(j.source), sortValue: (j) => j.source },
  { id: "agent_type", header: "Agent type", cell: (j) => text(j.agent_type), sortValue: (j) => j.agent_type ?? "" },
  { id: "started", header: "Started", cell: (j) => when(j.started_at), sortValue: (j) => j.started_at ?? "", nowrap: true },
  { id: "finished", header: "Finished", cell: (j) => when(j.finished_at), sortValue: (j) => j.finished_at ?? "", nowrap: true },
  { id: "input_tokens", header: "Input tokens", cell: (j) => count(j.input_tokens), sortValue: (j) => j.input_tokens ?? -1, nowrap: true },
  { id: "output_tokens", header: "Output tokens", cell: (j) => count(j.output_tokens), sortValue: (j) => j.output_tokens ?? -1, nowrap: true },
  { id: "error_class", header: "Error class", cell: (j) => text(j.error_class), sortValue: (j) => j.error_class ?? "", nowrap: true },
];

/** Known ids only, in ORDER; a value that leaves none (corrupt, renamed ids) is the default. */
function sanitize(stored: unknown): string[] {
  const ids = Array.isArray(stored) ? ORDER.filter((id) => stored.includes(id)) : [];
  return ids.length ? ids : DEFAULT_VISIBLE;
}
const parse = (raw: string | undefined) => { try { return sanitize(JSON.parse(raw ?? "")); } catch { return DEFAULT_VISIBLE; } };
const same = (a: string[], b: string[]) => a.length === b.length && a.every((id, i) => id === b[i]);

function ColumnPicker({ headers, visible, onChange }: {
  headers: Map<string, string>; visible: string[]; onChange: (ids: string[]) => void;
}) {
  const isDefault = same(visible, DEFAULT_VISIBLE);
  return (
    <Popover position="bottom-end" withArrow shadow="md" trapFocus returnFocus width={240}>
      {/* The dot says the table is not the default one. The dialog takes its name from the button. */}
      <Indicator disabled={isDefault} size={8} offset={6} withBorder>
        <Popover.Target>
          <IconAction label="Columns" icon={<Columns3 size={16} />} />
        </Popover.Target>
      </Indicator>
      <Popover.Dropdown>
        <Stack gap="xs">
          <Group justify="space-between" wrap="nowrap">
            <Text fw={600} size="sm">Columns</Text>
            <Group gap="xs" wrap="nowrap">
              <IconAction label="Show all columns" icon={<ListChecks size={16} />}
                disabled={visible.length === ORDER.length} onClick={() => onChange(ORDER)} />
              <IconAction label="Reset columns" icon={<RotateCcw size={16} />}
                disabled={isDefault} onClick={() => onChange(DEFAULT_VISIBLE)} />
            </Group>
          </Group>
          <Checkbox.Group value={visible} onChange={(ids) => onChange(sanitize(ids))}>
            <Stack gap="xs">
              {ORDER.map((id) => {
                const last = visible.length === 1 && visible[0] === id;
                return <Checkbox key={id} value={id} size="sm" label={headers.get(id)} disabled={last}
                  description={last ? "At least one column stays visible" : undefined} />;
              })}
            </Stack>
          </Checkbox.Group>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}

function JobDrawer({ id, onClose }: { id: number | null; onClose: () => void }) {
  const job = useQuery({
    queryKey: ["admin-job", id], enabled: id !== null,
    queryFn: ({ signal }) => apiFetch<JobDetailData>(`/api/admin/jobs/${id}`, { signal }),
    refetchInterval: LIVE_REFETCH_MS,
  });
  const navigate = useNavigate();
  const links = useJobLinks(id).map((l) => <OpenAction key={l.to} label={l.label} onClick={() => void navigate(l.to)} />);
  return (
    <SideDrawer opened={id !== null} onClose={onClose} title={`Job ${id ?? ""}`} actions={links}>
      {job.isPending ? <LoadingState /> : job.error ? <ErrorState message={message(job.error)} /> : job.data && <JobDetail job={job.data} />}
    </SideDrawer>
  );
}

/** What the empty result was filtered by, in words: `Status FAILED and “abc”`. */
function constraints(status: string, q: string) {
  return [status !== "All" && `Status ${status}`, q && `“${q}”`].filter(Boolean).join(" and ");
}

export function Jobs() {
  const [params] = useSearchParams();
  const grid = useGridParams({ defaultSort: DEFAULT_SORT, sorts: ORDER, filters: { status: STATUSES.slice(1) } });
  const status = grid.filters.status ?? "All";
  // `?job=<id>` opens that job (a conversation's runs link here).
  const [openId, setOpenId] = useState<number | null>(() => Number(params.get("job")) || null);
  const jobs = useQuery({
    queryKey: ["admin-jobs", grid.query],
    queryFn: ({ signal }) => apiFetch<JobPage>(`/api/admin/jobs?${grid.query}`, { signal }),
    placeholderData: keepPreviousData,
    refetchInterval: LIVE_REFETCH_MS,
  });
  const answer = jobs.data;
  // Only an answer for the query in the URL may move the URL: a kept previous answer has another total.
  const current = answer && !jobs.isPlaceholderData && answer.page === grid.page ? answer : undefined;
  const last = current ? lastPage(current.total, current.page_size) : null;
  useEffect(() => {
    if (last !== null && grid.page > last) grid.setPage(last, { replace: true });
  }, [last, grid.page]); // eslint-disable-line react-hooks/exhaustive-deps -- grid is new each render
  const open = (j: JobHead) => setOpenId(j.id);
  const [visible, setVisible] = useLocalStorage<string[]>({
    key: STORAGE_KEY, defaultValue: DEFAULT_VISIBLE, getInitialValueInEffect: false,
    deserialize: parse, serialize: JSON.stringify,
  });
  const defined = jobColumns(open);
  const actions = defined.filter((c) => c.id === "actions");
  const byId = new Map<string, Column<JobRow>>([...defined, ...EXTRA].filter((c) => c.id !== "actions").map((c) => [c.id, c]));
  const shown = sanitize(visible);
  const columns = [...shown.map((id) => byId.get(id)!), ...actions];
  const filtered = constraints(status, grid.q);
  return (
    <div className="ag-stack">
      <PageHeader title="Jobs" description="The job queue. Activate a row for its detail." />
      <Group gap="sm" align="flex-end">
        {/* Six statuses are wider than a phone: the row scrolls sideways. The wrapper scrolls, not the
            control, or Mantine's indicator is drawn off its option. */}
        <Box maw="100%" style={{ overflowX: "auto" }}>
          <SegmentedControl aria-label="Status" data={STATUSES} value={status} miw="max-content"
            onChange={(v) => grid.setFilter("status", v === "All" ? null : v)} />
        </Box>
        <TextInput aria-label="Search jobs" placeholder="ID, type, view or reference" value={grid.search} w={300} error={grid.searchError}
          leftSection={<Search size={16} aria-hidden />}
          rightSection={jobs.isPlaceholderData ? <Loader size="xs" aria-label="Searching" /> : null}
          onChange={(e) => grid.setSearch(e.currentTarget.value)} />
        <Group ml="auto">
          <ColumnPicker headers={new Map([...byId].map(([id, c]) => [id, c.header]))} visible={shown} onChange={setVisible} />
        </Group>
      </Group>
      <DataTable caption="Jobs" columns={columns} rows={answer?.rows} rowKey={(j) => String(j.id)} onRowActivate={open}
        sort={grid.sort} onSortChange={grid.setSort}
        loading={jobs.isPending} error={jobs.error ? message(jobs.error) : null} onRetry={() => void jobs.refetch()}
        emptyTitle={filtered ? "No matching jobs" : "No jobs"}
        emptyText={filtered ? `No job matches ${filtered}.` : undefined}
        emptyAction={filtered ? <Button onClick={grid.clear}>Clear filters</Button> : undefined} />
      {answer && <Pager page={answer.page} pageSize={answer.page_size} total={answer.total} onPage={grid.setPage} />}
      <JobDrawer id={openId} onClose={() => setOpenId(null)} />
    </div>
  );
}
