import { useState } from "react";
import { Eye } from "lucide-react";
import { Group, SegmentedControl, Text, TextInput } from "@mantine/core";
import { useSearchParams } from "react-router";
import { apiFetch, LIVE_REFETCH_MS, useQuery } from "@agento/api";
import {
  DataTable, durationOf, ErrorState, formatDuration, IconAction, JobDetail, jobTone, LoadingState, PageHeader, SideDrawer,
  StatusBadge, Timestamp, type Column, type JobDetailData,
} from "@agento/ui";
import { message, whole, type JobHead, type JobRow } from "./shared";

const STATUSES = ["All", "TODO", "RUNNING", "SUCCESS", "FAILED", "DEAD"];
// The same fields the TUI search reads (framework/admin/screens/jobs.py).
const SEARCHED = ["id", "type", "status", "agent_view_code", "reference_id"] as const;
const when = (v: string | null | undefined) => (v ? <Timestamp value={v} /> : "—");

const seconds = (j: JobHead) => formatDuration(durationOf(j));

export const jobColumns = (open?: (j: JobHead) => void): Column<JobHead>[] => [
  { id: "id", header: "ID", cell: (j) => j.id, sortValue: (j) => j.id },
  { id: "type", header: "Type", cell: (j) => j.type, sortValue: (j) => j.type },
  { id: "status", header: "Status", cell: (j) => whole(<StatusBadge tone={jobTone(j.status)}>{j.status}</StatusBadge>), sortValue: (j) => j.status },
  { id: "view", header: "Agent view", cell: (j) => j.agent_view_code ?? "—", sortValue: (j) => j.agent_view_code ?? "" },
  { id: "ref", header: "Reference", cell: (j) => <Text size="sm" lineClamp={2} style={{ overflowWrap: "anywhere" }}>{j.reference_id ?? "—"}</Text>, sortValue: (j) => j.reference_id ?? "" },
  { id: "created", header: "Created", cell: (j) => when(j.created_at), sortValue: (j) => j.created_at ?? "" },
  { id: "duration", header: "Duration", cell: seconds, sortValue: (j) => durationOf(j) ?? -1 },
  ...(open ? [{ id: "actions", header: "Actions", cell: (j: JobHead) => <IconAction label="Details" icon={<Eye size={16} />} onClick={() => open(j)} /> }] : []),
];

function JobDrawer({ id, onClose }: { id: number | null; onClose: () => void }) {
  const job = useQuery({
    queryKey: ["admin-job", id], enabled: id !== null,
    queryFn: ({ signal }) => apiFetch<JobDetailData>(`/api/admin/jobs/${id}`, { signal }),
    refetchInterval: LIVE_REFETCH_MS,
  });
  return (
    <SideDrawer opened={id !== null} onClose={onClose} title={`Job ${id ?? ""}`}>
      {job.isPending ? <LoadingState /> : job.error ? <ErrorState message={message(job.error)} /> : job.data && <JobDetail job={job.data} />}
    </SideDrawer>
  );
}

export function Jobs() {
  const [params, setParams] = useSearchParams();
  const status = STATUSES.includes(params.get("status") ?? "") ? params.get("status")! : "All";
  const [search, setSearch] = useState("");
  // `?job=<id>` opens that job (a conversation's runs link here).
  const [openId, setOpenId] = useState<number | null>(() => Number(params.get("job")) || null);
  const jobs = useQuery({
    queryKey: ["admin-jobs", status],
    queryFn: ({ signal }) => apiFetch<JobRow[]>(`/api/admin/jobs${status === "All" ? "" : `?status=${status}`}`, { signal }),
    refetchInterval: LIVE_REFETCH_MS,
  });
  const needle = search.trim().toLowerCase();
  const rows = jobs.data?.filter((j) => !needle || SEARCHED.map((k) => String(j[k] ?? "")).join(" ").toLowerCase().includes(needle));
  const open = (j: JobHead) => setOpenId(j.id);
  return (
    <div className="ag-stack">
      <PageHeader title="Jobs" description="The job queue. Activate a row for its detail." />
      <Group gap="sm" align="flex-end">
        <SegmentedControl aria-label="Status" data={STATUSES} value={status}
          onChange={(v) => setParams((p) => { const n = new URLSearchParams(p); if (v === "All") n.delete("status"); else n.set("status", v); return n; })} />
        <TextInput aria-label="Search jobs" placeholder="Search…" value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
      </Group>
      <DataTable caption="Jobs" columns={jobColumns(open)} rows={rows} rowKey={(j) => String(j.id)} onRowActivate={open}
        loading={jobs.isPending} error={jobs.error ? message(jobs.error) : null} onRetry={() => void jobs.refetch()}
        emptyTitle="No jobs" />
      <JobDrawer id={openId} onClose={() => setOpenId(null)} />
    </div>
  );
}
