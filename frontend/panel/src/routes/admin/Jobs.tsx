import { useEffect, useState } from "react";
import { Columns3, Eye, ListChecks, RotateCcw, Search } from "lucide-react";
import { Box, Checkbox, Group, Indicator, Loader, Popover, SegmentedControl, Stack, Text, TextInput } from "@mantine/core";
import { useLocalStorage } from "@mantine/hooks";
import { useNavigate, useSearchParams } from "react-router";
import { apiFetch, keepPreviousData, LIVE_REFETCH_MS, useErrorText, useJobLinks, useQuery } from "@agento/api";
import {
  Button, DataTable, durationOf, ErrorState, IconAction, JobDetail, jobTone, lastPage, LoadingState, OpenAction,
  PageHeader, Pager, SideDrawer, StatusBadge, Timestamp, useFormat, useGridParams, useJobStatusText, useT, type Column, type JobDetailData,
  type Translate,
} from "@agento/ui";
import { whole, type JobHead, type JobRow } from "./shared";

const ALL = "All";
const STATUSES = ["TODO", "RUNNING", "SUCCESS", "FAILED", "DEAD"];
const DEFAULT_SORT = { id: "id", desc: true };
interface JobPage { rows: JobRow[]; total: number; page: number; page_size: number }
const when = (v: string | null | undefined) => (v ? <Timestamp value={v} /> : "—");

export function useJobColumns(open?: (j: JobHead) => void): Column<JobHead>[] {
  const t = useT("panel");
  const statusText = useJobStatusText();
  const { duration } = useFormat();
  return [
    { id: "id", header: t("jobs.column.id"), cell: (j) => j.id, sortValue: (j) => j.id },
    { id: "type", header: t("jobs.column.type"), cell: (j) => j.type, sortValue: (j) => j.type },
    { id: "status", header: t("jobs.column.status"), cell: (j) => whole(<StatusBadge tone={jobTone(j.status)}>{statusText(j.status)}</StatusBadge>), sortValue: (j) => j.status },
    { id: "view", header: t("jobs.column.view"), cell: (j) => j.agent_view_code ?? "—", sortValue: (j) => j.agent_view_code ?? "" },
    { id: "ref", header: t("jobs.column.ref"), cell: (j) => <Text size="sm" lineClamp={2} style={{ overflowWrap: "break-word" }}>{j.reference_id ?? "—"}</Text>, sortValue: (j) => j.reference_id ?? "" },
    { id: "created", header: t("jobs.column.created"), cell: (j) => when(j.created_at), sortValue: (j) => j.created_at ?? "", nowrap: true },
    { id: "duration", header: t("jobs.column.duration"), cell: (j) => duration(durationOf(j)), sortValue: (j) => durationOf(j) ?? -1 },
    ...(open ? [{ id: "actions", header: t("jobs.column.actions"), cell: (j: JobHead) => <IconAction label={t("jobs.details")} icon={<Eye size={16} />} onClick={() => open(j)} /> }] : []),
  ];
}

// The order of the columns, stated once: the table and the picker both read it. Actions is not
// here: it is always the last column, and the only mouse path to a job's detail.
const ORDER = ["id", "type", "status", "view", "source", "agent_type", "ref",
  "created", "started", "finished", "duration", "input_tokens", "output_tokens", "error_class"];
const DEFAULT_VISIBLE = ["id", "type", "status", "view", "ref", "created", "duration"];
// Per browser: a viewer's convenience, not shared state. Mantine's hook catches a storage that throws.
const STORAGE_KEY = "agento.admin.jobs.columns";

const text = (v: string | null) => v ?? "—";
// The columns the API sends but the Dashboard does not have (JobRow fields only).
const extraColumns = (t: Translate, count: (v: number | null) => string): Column<JobRow>[] => [
  { id: "source", header: t("jobs.column.source"), cell: (j) => text(j.source), sortValue: (j) => j.source },
  { id: "agent_type", header: t("jobs.column.agent_type"), cell: (j) => text(j.agent_type), sortValue: (j) => j.agent_type ?? "" },
  { id: "started", header: t("jobs.column.started"), cell: (j) => when(j.started_at), sortValue: (j) => j.started_at ?? "", nowrap: true },
  { id: "finished", header: t("jobs.column.finished"), cell: (j) => when(j.finished_at), sortValue: (j) => j.finished_at ?? "", nowrap: true },
  { id: "input_tokens", header: t("jobs.column.input_tokens"), cell: (j) => count(j.input_tokens), sortValue: (j) => j.input_tokens ?? -1, nowrap: true },
  { id: "output_tokens", header: t("jobs.column.output_tokens"), cell: (j) => count(j.output_tokens), sortValue: (j) => j.output_tokens ?? -1, nowrap: true },
  { id: "error_class", header: t("jobs.column.error_class"), cell: (j) => text(j.error_class), sortValue: (j) => j.error_class ?? "", nowrap: true },
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
  const t = useT("panel");
  const isDefault = same(visible, DEFAULT_VISIBLE);
  return (
    <Popover position="bottom-end" withArrow shadow="md" trapFocus returnFocus width={240}>
      {/* The dot says the table is not the default one. The dialog takes its name from the button. */}
      <Indicator disabled={isDefault} size={8} offset={6} withBorder>
        <Popover.Target>
          <IconAction label={t("jobs.columns")} icon={<Columns3 size={16} />} />
        </Popover.Target>
      </Indicator>
      <Popover.Dropdown>
        <Stack gap="xs">
          <Group justify="space-between" wrap="nowrap">
            <Text fw={600} size="sm">{t("jobs.columns")}</Text>
            <Group gap="xs" wrap="nowrap">
              <IconAction label={t("jobs.show_all_columns")} icon={<ListChecks size={16} />}
                disabled={visible.length === ORDER.length} onClick={() => onChange(ORDER)} />
              <IconAction label={t("jobs.reset_columns")} icon={<RotateCcw size={16} />}
                disabled={isDefault} onClick={() => onChange(DEFAULT_VISIBLE)} />
            </Group>
          </Group>
          <Checkbox.Group value={visible} onChange={(ids) => onChange(sanitize(ids))}>
            <Stack gap="xs">
              {ORDER.map((id) => {
                const last = visible.length === 1 && visible[0] === id;
                return <Checkbox key={id} value={id} size="sm" label={headers.get(id)} disabled={last}
                  description={last ? t("jobs.last_column") : undefined} />;
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
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const links = useJobLinks(id).map((l) => <OpenAction key={l.to} label={l.label} onClick={() => void navigate(l.to)} />);
  return (
    <SideDrawer opened={id !== null} onClose={onClose} title={t("jobs.drawer_title", { id: id === null ? "" : String(id) })} actions={links}>
      {job.isPending ? <LoadingState /> : job.error ? <ErrorState message={errorText(job.error, "jobs.detail_failed")} /> : job.data && <JobDetail job={job.data} />}
    </SideDrawer>
  );
}

/** What the empty result was filtered by, in words; `undefined` with no filter. */
function noMatchText(t: Translate, status: string | undefined, q: string) {
  if (status && q) return t("jobs.no_match.both", { status, q });
  if (status) return t("jobs.no_match.status", { status });
  return q ? t("jobs.no_match.search", { q }) : undefined;
}

export function Jobs() {
  const [params] = useSearchParams();
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const { number } = useFormat();
  const grid = useGridParams({ defaultSort: DEFAULT_SORT, sorts: ORDER, filters: { status: STATUSES } });
  const status = grid.filters.status ?? ALL;
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
  const defined = useJobColumns(open);
  const statusText = useJobStatusText();
  const actions = defined.filter((c) => c.id === "actions");
  const count = (v: number | null) => (v === null ? "—" : number(v));
  const extra = extraColumns(t, count);
  const byId = new Map<string, Column<JobRow>>([...defined, ...extra].filter((c) => c.id !== "actions").map((c) => [c.id, c]));
  const shown = sanitize(visible);
  const columns = [...shown.map((id) => byId.get(id)!), ...actions];
  const noMatch = noMatchText(t, grid.filters.status ? statusText(grid.filters.status) : undefined, grid.q);
  return (
    <div className="ag-stack">
      <PageHeader title={t("jobs.title")} description={t("jobs.description")} />
      <Group gap="sm" align="flex-end">
        {/* Six statuses are wider than a phone: the row scrolls sideways. The wrapper scrolls, not the
            control, or Mantine's indicator is drawn off its option. */}
        <Box maw="100%" style={{ overflowX: "auto" }}>
          <SegmentedControl aria-label={t("jobs.status.label")} data={[{ value: ALL, label: t("jobs.status.all") }, ...STATUSES.map((s) => ({ value: s, label: statusText(s) }))]} value={status} miw="max-content"
            onChange={(v) => grid.setFilter("status", v === ALL ? null : v)} />
        </Box>
        <TextInput aria-label={t("jobs.search.label")} placeholder={t("jobs.search.placeholder")} value={grid.search} w={300} error={grid.searchError}
          leftSection={<Search size={16} aria-hidden />}
          rightSection={jobs.isPlaceholderData ? <Loader size="xs" aria-label={t("jobs.searching")} /> : null}
          onChange={(e) => grid.setSearch(e.currentTarget.value)} />
        <Group ml="auto">
          <ColumnPicker headers={new Map([...byId].map(([id, c]) => [id, c.header]))} visible={shown} onChange={setVisible} />
        </Group>
      </Group>
      <DataTable caption={t("jobs.title")} columns={columns} rows={answer?.rows} rowKey={(j) => String(j.id)} onRowActivate={open}
        sort={grid.sort} onSortChange={grid.setSort}
        loading={jobs.isPending} error={jobs.error ? errorText(jobs.error, "jobs.load_failed") : null} onRetry={() => void jobs.refetch()}
        emptyTitle={noMatch ? t("jobs.no_match.title") : t("jobs.empty")}
        emptyText={noMatch}
        emptyAction={noMatch ? <Button onClick={grid.clear}>{t("jobs.clear_filters")}</Button> : undefined} />
      {answer && <Pager page={answer.page} pageSize={answer.page_size} total={answer.total} onPage={grid.setPage} />}
      <JobDrawer id={openId} onClose={() => setOpenId(null)} />
    </div>
  );
}
