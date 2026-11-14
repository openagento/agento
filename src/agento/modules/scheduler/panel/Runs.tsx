// The schedule's last runs (`GET …/runs`), each with a quick look at its answer and, for a user
// who may read jobs (`admin.jobs`), the job itself — both in a drawer that keeps the form.
import { useState } from "react";
import { useNavigate } from "react-router";
import { apiFetch, LIVE_REFETCH_MS, useCan, useQuery } from "@agento/api";
import {
  ChatMessage, DataTable, DetailsAction, durationOf, EmptyState, ErrorState, formatDuration, JobDetail, jobTone,
  LoadingState, OpenAction, PreviewAction, SideDrawer, StatusBadge, Timestamp, type Column, type JobDetailData,
} from "@agento/ui";
import { message, useRuns, type Run, type Schedule } from "./api";

const SNIPPET = 90;
const snippet = (text: string) => {
  // One plain line: the table shows no Markdown, so its marks would show as text.
  const line = text.replace(/[*_`#>]+/g, "").replace(/\s+/g, " ").trim();
  return line.length > SNIPPET ? `${line.slice(0, SNIPPET)}…` : line;
};
const startOf = (r: Run) => r.started_at ?? r.created_at;
const waiting = (r: Run) => r.status === "TODO" || r.status === "RUNNING";

export function LastRuns({ row }: { row: Schedule }) {
  const runs = useRuns(row.id);
  // The admin job route refuses a session without the leaf, so the icon shows only with it.
  const jobs = useCan("admin.jobs");
  const [preview, setPreview] = useState<Run | null>(null);
  const [details, setDetails] = useState<number | null>(null);
  const columns: Column<Run>[] = [
    { id: "status", header: "Status", nowrap: true, cell: (r) => <StatusBadge tone={jobTone(r.status)}>{r.status}</StatusBadge> },
    { id: "started", header: "Started", nowrap: true, cell: (r) => { const at = startOf(r); return at ? <Timestamp value={at} /> : "—"; } },
    { id: "duration", header: "Duration", nowrap: true, cell: (r) => formatDuration(durationOf(r)) },
    { id: "answer", header: "Answer", prose: true, cell: (r) => (r.answer ? snippet(r.answer)
      : <span className="ag-muted">{waiting(r) ? "Running…" : "No answer"}</span>) },
    { id: "actions", header: "Actions", nowrap: true, cell: (r) => (
      <div className="ag-row">
        <PreviewAction label={`Preview run ${r.id}`} onClick={() => setPreview(r)} />
        {jobs && <DetailsAction label={`Job ${r.id} details`} onClick={() => setDetails(r.id)} />}
      </div>
    ) },
  ];
  // The row the poll refreshed, so an open preview follows its run to the end.
  const shown = preview && (runs.data?.find((r) => r.id === preview.id) ?? preview);
  return (
    <>
      <DataTable caption="Last runs" columns={columns} rows={runs.data} rowKey={(r) => String(r.id)}
        loading={runs.isPending} error={runs.error ? message(runs.error) : null} onRetry={() => void runs.refetch()}
        onRowActivate={setPreview} emptyTitle="No runs yet" emptyText="The first run shows here when it starts." />
      {shown && <RunPreview run={shown} conversationId={row.conversation_id} onClose={() => setPreview(null)} />}
      {details !== null && <JobDrawer id={details} onClose={() => setDetails(null)} />}
    </>
  );
}

function RunPreview({ run, conversationId, onClose }: { run: Run; conversationId: number | null; onClose: () => void }) {
  const navigate = useNavigate();
  const at = startOf(run);
  return (
    <SideDrawer opened onClose={onClose} title={`Run ${run.id}`}
      actions={conversationId !== null && (
        <OpenAction label="Open conversation" onClick={() => navigate(`/conversations/${conversationId}`)} />
      )}>
      <div className="ag-stack">
        <div className="ag-row">
          <StatusBadge tone={jobTone(run.status)}>{run.status}</StatusBadge>
          <span className="ag-muted">
            {at ? <Timestamp value={at} /> : "Not started"}{durationOf(run) !== null && ` · ${formatDuration(durationOf(run))}`}
          </span>
        </div>
        {run.answer ? <ChatMessage author="assistant" text={run.answer} time={run.finished_at} />
          : waiting(run) ? <LoadingState label="The run has not ended yet." />
            : <EmptyState title="No answer">This run wrote nothing into the conversation.</EmptyState>}
      </div>
    </SideDrawer>
  );
}

function JobDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const job = useQuery({
    queryKey: ["scheduler", "job", id], refetchInterval: LIVE_REFETCH_MS,
    queryFn: ({ signal }) => apiFetch<JobDetailData>(`/api/admin/jobs/${id}`, { signal }),
  });
  return (
    <SideDrawer opened onClose={onClose} title={`Job ${id}`}>
      {job.isPending ? <LoadingState /> : job.error ? <ErrorState message={message(job.error)} /> : <JobDetail job={job.data} />}
    </SideDrawer>
  );
}
