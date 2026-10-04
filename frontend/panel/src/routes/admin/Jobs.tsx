import { useState, type ReactNode } from "react";
import { Drawer, Group, SegmentedControl, Stack, Table, Text, TextInput } from "@mantine/core";
import { useSearchParams } from "react-router";
import { apiFetch, useQuery } from "@agento/api";
import {
  Button, CodeBlock, DataTable, ErrorState, LoadingState, PageHeader, SectionHeader, StatusBadge, Timestamp, type Column,
} from "@agento/ui";
import { AdminOnly, durationOf, jobTone, message, whole, type JobHead, type JobRow } from "./shared";

interface JobDetail extends JobRow {
  model: string | null; error_message: string | null; result_summary: string | null; prompt: string | null; output: string | null;
}

const STATUSES = ["All", "TODO", "RUNNING", "SUCCESS", "FAILED", "DEAD"];
// The same fields the TUI search reads (framework/admin/screens/jobs.py).
const SEARCHED = ["id", "type", "status", "agent_view_code", "reference_id"] as const;
const when = (v: string | null | undefined) => (v ? <Timestamp value={v} /> : "—");

const seconds = (j: JobHead) => { const d = durationOf(j); return d === null ? "—" : `${d}s`; };

export const jobColumns = (open?: (j: JobHead) => void): Column<JobHead>[] => [
  { id: "id", header: "ID", cell: (j) => j.id, sortValue: (j) => j.id },
  { id: "type", header: "Type", cell: (j) => j.type, sortValue: (j) => j.type },
  { id: "status", header: "Status", cell: (j) => whole(<StatusBadge tone={jobTone(j.status)}>{j.status}</StatusBadge>), sortValue: (j) => j.status },
  { id: "view", header: "Agent view", cell: (j) => j.agent_view_code ?? "—", sortValue: (j) => j.agent_view_code ?? "" },
  { id: "ref", header: "Reference", cell: (j) => <Text size="sm" lineClamp={2} style={{ overflowWrap: "anywhere" }}>{j.reference_id ?? "—"}</Text>, sortValue: (j) => j.reference_id ?? "" },
  { id: "created", header: "Created", cell: (j) => when(j.created_at), sortValue: (j) => j.created_at ?? "" },
  { id: "duration", header: "Duration", cell: seconds, sortValue: (j) => durationOf(j) ?? -1 },
  ...(open ? [{ id: "actions", header: "Actions", cell: (j: JobHead) => <Button variant="subtle" onClick={() => open(j)}>Details</Button> }] : []),
];

function JobDrawer({ id, onClose }: { id: number | null; onClose: () => void }) {
  const job = useQuery({
    queryKey: ["admin-job", id], enabled: id !== null,
    queryFn: ({ signal }) => apiFetch<JobDetail>(`/api/admin/jobs/${id}`, { signal }),
  });
  const j = job.data;
  const facts: [string, ReactNode][] = j ? [
    ["Status", <StatusBadge key="s" tone={jobTone(j.status)}>{j.status}</StatusBadge>], ["Type", j.type], ["Source", j.source],
    ["Agent view", j.agent_view_code ?? "—"], ["Reference", j.reference_id ?? "—"], ["Agent", j.agent_type ?? "—"],
    ["Model", j.model ?? "—"], ["Tokens in / out", `${j.input_tokens ?? 0} / ${j.output_tokens ?? 0}`],
    ["Created", when(j.created_at)], ["Started", when(j.started_at)], ["Finished", when(j.finished_at)],
    ["Duration", seconds(j)], ...(j.error_class ? [["Error class", j.error_class] as [string, ReactNode]] : []),
  ] : [];
  return (
    <Drawer opened={id !== null} onClose={onClose} position="right" size="lg" title={`Job ${id ?? ""}`}
      closeButtonProps={{ "aria-label": "Close" }}>
      {job.isPending ? <LoadingState /> : job.error ? <ErrorState message={message(job.error)} /> : j && (
        <Stack gap="md">
          <Table>
            <Table.Tbody>
              {facts.map(([k, v]) => <Table.Tr key={k}><Table.Th scope="row">{k}</Table.Th><Table.Td>{v}</Table.Td></Table.Tr>)}
            </Table.Tbody>
          </Table>
          {j.error_message && <><SectionHeader>Error</SectionHeader><CodeBlock code={j.error_message} /></>}
          {j.result_summary && <><SectionHeader>Result</SectionHeader><Text size="sm">{j.result_summary}</Text></>}
          <SectionHeader>Prompt</SectionHeader><CodeBlock code={j.prompt || "—"} />
          <SectionHeader>Output</SectionHeader><CodeBlock code={j.output || "—"} />
          <SectionHeader>Replay</SectionHeader>
          <Text size="sm" c="dimmed">Runs in the cron container. Use the TUI or the CLI.</Text>
          <CodeBlock code={`bin/agento replay ${j.id}`} copy />
        </Stack>
      )}
    </Drawer>
  );
}

export function Jobs() {
  const [params, setParams] = useSearchParams();
  const status = STATUSES.includes(params.get("status") ?? "") ? params.get("status")! : "All";
  const [search, setSearch] = useState("");
  const [openId, setOpenId] = useState<number | null>(null);
  const jobs = useQuery({
    queryKey: ["admin-jobs", status],
    queryFn: ({ signal }) => apiFetch<JobRow[]>(`/api/admin/jobs${status === "All" ? "" : `?status=${status}`}`, { signal }),
  });
  const needle = search.trim().toLowerCase();
  const rows = jobs.data?.filter((j) => !needle || SEARCHED.map((k) => String(j[k] ?? "")).join(" ").toLowerCase().includes(needle));
  const open = (j: JobHead) => setOpenId(j.id);
  return (
    <AdminOnly>
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
    </AdminOnly>
  );
}
