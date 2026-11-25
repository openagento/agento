// The schedule's last runs (`GET …/runs`), each with a quick look at its answer and, for a user
// who may read jobs (`admin.jobs`), the job itself — both in a drawer that keeps the form.
import { useState } from "react";
import { useNavigate } from "react-router";
import { apiFetch, LIVE_REFETCH_MS, useCan, useErrorText, useJobLinks, useQuery } from "@agento/api";
import {
  ChatMessage, DataTable, DetailsAction, durationOf, EmptyState, ErrorState, JobDetail, jobTone,
  LoadingState, OpenAction, PreviewAction, SideDrawer, StatusBadge, Timestamp, useFormat, useJobStatusText, useT, type Column, type JobDetailData,
} from "@agento/ui";
import { useRuns, type Run, type Schedule } from "./api";

const SNIPPET = 90;
const snippet = (text: string) => {
  // One plain line: the table shows no Markdown, so its marks would show as text.
  const line = text.replace(/[*_`#>]+/g, "").replace(/\s+/g, " ").trim();
  return line.length > SNIPPET ? `${line.slice(0, SNIPPET)}…` : line;
};
const startOf = (r: Run) => r.started_at ?? r.created_at;
const waiting = (r: Run) => r.status === "TODO" || r.status === "RUNNING";

export function LastRuns({ row }: { row: Schedule }) {
  const t = useT("scheduler");
  const errorText = useErrorText("scheduler");
  const statusText = useJobStatusText();
  const { duration } = useFormat();
  const runs = useRuns(row.id);
  // The admin job route refuses a session without the leaf, so the icon shows only with it.
  const jobs = useCan("admin.jobs");
  const [preview, setPreview] = useState<Run | null>(null);
  const [details, setDetails] = useState<number | null>(null);
  const columns: Column<Run>[] = [
    { id: "status", header: t("runs.column.status"), nowrap: true, cell: (r) => <StatusBadge tone={jobTone(r.status)}>{statusText(r.status)}</StatusBadge> },
    { id: "started", header: t("runs.column.started"), nowrap: true, cell: (r) => { const at = startOf(r); return at ? <Timestamp value={at} /> : "—"; } },
    { id: "duration", header: t("runs.column.duration"), nowrap: true, cell: (r) => duration(durationOf(r)) },
    { id: "answer", header: t("runs.column.answer"), prose: true, cell: (r) => (r.answer ? snippet(r.answer)
      : <span className="ag-muted">{waiting(r) ? t("runs.running") : t("runs.no_answer")}</span>) },
    { id: "actions", header: t("runs.column.actions"), nowrap: true, cell: (r) => (
      <div className="ag-row">
        <PreviewAction label={t("runs.preview", { id: String(r.id) })} onClick={() => setPreview(r)} />
        {jobs && <DetailsAction label={t("runs.job_details", { id: String(r.id) })} onClick={() => setDetails(r.id)} />}
      </div>
    ) },
  ];
  // The row the poll refreshed, so an open preview follows its run to the end.
  const shown = preview && (runs.data?.find((r) => r.id === preview.id) ?? preview);
  return (
    <>
      <DataTable caption={t("runs.caption")} columns={columns} rows={runs.data} rowKey={(r) => String(r.id)}
        loading={runs.isPending} error={runs.error ? errorText(runs.error, "error.generic") : null} onRetry={() => void runs.refetch()}
        onRowActivate={setPreview} emptyTitle={t("runs.empty.title")} emptyText={t("runs.empty.text")} />
      {shown && <RunPreview run={shown} conversationId={row.conversation_id} onClose={() => setPreview(null)} />}
      {details !== null && <JobDrawer id={details} onClose={() => setDetails(null)} />}
    </>
  );
}

function RunPreview({ run, conversationId, onClose }: { run: Run; conversationId: number | null; onClose: () => void }) {
  const t = useT("scheduler");
  const statusText = useJobStatusText();
  const { duration } = useFormat();
  const navigate = useNavigate();
  const at = startOf(run);
  return (
    <SideDrawer opened onClose={onClose} title={t("runs.run", { id: String(run.id) })}
      actions={conversationId !== null && (
        <OpenAction label={t("action.open_conversation")} onClick={() => navigate(`/conversations/${conversationId}`)} />
      )}>
      <div className="ag-stack">
        <div className="ag-row">
          <StatusBadge tone={jobTone(run.status)}>{statusText(run.status)}</StatusBadge>
          <span className="ag-muted">
            {at ? <Timestamp value={at} /> : t("runs.not_started")}{durationOf(run) !== null && ` · ${duration(durationOf(run))}`}
          </span>
        </div>
        {run.answer ? <ChatMessage author="assistant" text={run.answer} time={run.finished_at} />
          : waiting(run) ? <LoadingState label={t("runs.not_ended")} />
            : <EmptyState title={t("runs.no_answer")}>{t("runs.no_answer_text")}</EmptyState>}
      </div>
    </SideDrawer>
  );
}

function JobDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const t = useT("scheduler");
  const errorText = useErrorText("scheduler");
  const job = useQuery({
    queryKey: ["scheduler", "job", id], refetchInterval: LIVE_REFETCH_MS,
    queryFn: ({ signal }) => apiFetch<JobDetailData>(`/api/admin/jobs/${id}`, { signal }),
  });
  const navigate = useNavigate();
  const links = useJobLinks(id).map((l) => <OpenAction key={l.to} label={l.label} onClick={() => navigate(l.to)} />);
  return (
    <SideDrawer opened onClose={onClose} title={t("runs.job", { id: String(id) })} actions={links}>
      {job.isPending ? <LoadingState /> : job.error ? <ErrorState message={errorText(job.error, "error.generic")} /> : <JobDetail job={job.data} />}
    </SideDrawer>
  );
}
