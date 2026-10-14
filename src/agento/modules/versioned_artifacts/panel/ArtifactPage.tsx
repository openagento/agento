// One artifact (plan §2.5): its versions with the audit history, make current (a rollback too),
// and delete, and the app approvals per view. The API checks each leaf; this page only hides what the user cannot do.
import { useState } from "react";
import { useNavigate, useParams } from "react-router";
import { apiFetch, useCan, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  AppLink, Button, DataTable, DeleteAction, MakeCurrentAction, PageHeader, SectionHeader, StatusBadge, Timestamp,
  type Column,
} from "@agento/ui";
import { BASE, message, useArtifacts } from "./api";
import { useDeleteArtifact } from "./delete";

interface Event { operation: string; job_id: number | null; agent_view_id: number | null; actor: string; description: string | null; created_at: string }
interface Version { version_id: string; preview_path: string | null; events: Event[] }
interface Published { current_version: string; preview_stale?: boolean }
interface Approval { agent_view_id: number; approved_version_id: string; allowed_actions: string[]; activated_by: string; activated_at: string }

export default function ArtifactPage() {
  const code = useParams().code ?? "";
  const url = `${BASE}/${encodeURIComponent(code)}`;
  const qc = useQueryClient();
  const navigate = useNavigate();
  const canPublish = useCan("versioned_artifacts.publish");
  const canDelete = useCan("versioned_artifacts.delete");
  const list = useArtifacts();
  const current = list.data?.find((a) => a.artifact_code === code)?.current_version ?? null;
  const versions = useQuery({
    queryKey: ["artifacts", code, "versions"],
    queryFn: ({ signal }) => apiFetch<Version[]>(`${url}/versions`, { signal }),
  });
  const [failure, setFailure] = useState<string | null>(null);
  const del = useDeleteArtifact(() => navigate("/artifacts"));
  // The version whose publish moved `current` but not the served tree (`preview_stale`).
  const [stale, setStale] = useState<string | null>(null);
  const makeCurrent = useMutation({
    mutationFn: ({ version, expected }: { version: string; expected: string | null }) =>
      apiFetch<Published>(`${url}/publish`, { method: "POST", json: { version_id: version, expected_current_version: expected } }),
    onSuccess: (r) => { setFailure(null); setStale(r.preview_stale ? r.current_version : null); },
    onError: (e) => setFailure(message(e)),
    onSettled: () => qc.invalidateQueries({ queryKey: ["artifacts"] }),
  });

  const columns: Column<Version>[] = [
    { id: "version", header: "Version", cell: (v) => (
      <span className="ag-row"><code>{v.version_id}</code>{v.version_id === current && <StatusBadge tone="succeeded">current</StatusBadge>}</span>
    ) },
    { id: "history", header: "History", cell: (v) => (
      <ul className="ag-stack">{v.events.map((e, i) => (
        <li key={i}>
          <Timestamp value={e.created_at} /> <code>{e.operation}</code>
          {e.agent_view_id !== null && ` view ${e.agent_view_id}`}{e.job_id !== null && ` job ${e.job_id}`} {e.actor}
          {e.description && <> · <span>{e.description}</span></>}
        </li>
      ))}</ul>
    ) },
    ...(canPublish ? [{ id: "actions", header: "Actions", cell: (v: Version) => (v.version_id === current || !current ? null
      : <MakeCurrentAction onClick={() => makeCurrent.mutate({ version: v.version_id, expected: current })} disabled={makeCurrent.isPending} />) }] : []),
  ];

  return (
    <div className="ag-stack">
      <PageHeader title={code} description="Versions of this artifact, newest first."
        actions={canDelete && <DeleteAction onClick={() => del.open(code)} />} />
      {(failure ?? del.failure) && <p className="ag-field__error" role="alert">{failure ?? del.failure}</p>}
      {stale && (
        <div className="ag-row">
          <p className="ag-field__error" role="alert"><code>{stale}</code> is current, but the site still shows the previous version.</p>
          {/* The repair is the ordinary publish with both ids equal to the new current. */}
          <Button onClick={() => makeCurrent.mutate({ version: stale, expected: stale })} disabled={makeCurrent.isPending}>Repair</Button>
        </div>
      )}
      <DataTable caption="Versions" columns={columns} rows={versions.data} rowKey={(v) => v.version_id}
        loading={versions.isPending} error={versions.error ? message(versions.error) : null}
        onRetry={() => void versions.refetch()} emptyTitle="No versions" />
      <Approvals code={code} />
      {del.dialog}
    </div>
  );
}

/** The app approvals per view, from the miniapps module. Any error (miniapps off, no `edit` on the app) shows nothing. */
function Approvals({ code }: { code: string }) {
  const approvals = useQuery({
    queryKey: ["artifacts", code, "approvals"],
    retry: false,
    queryFn: ({ signal }) => apiFetch<Approval[]>(`/api/miniapps/apps/${encodeURIComponent(code)}/approvals`, { signal }),
  });
  if (!approvals.data) return null;
  const columns: Column<Approval>[] = [
    { id: "view", header: "View", cell: (a) => <AppLink to={`/apps/${code}?view=${a.agent_view_id}`}>view {a.agent_view_id}</AppLink> },
    { id: "version", header: "Approved version", cell: (a) => <code>{a.approved_version_id}</code> },
    { id: "actions", header: "Actions", cell: (a) => <code>{a.allowed_actions.join(", ")}</code> },
    { id: "by", header: "Approved", cell: (a) => <>{a.activated_by}, <Timestamp value={a.activated_at} /></> },
  ];
  return (
    <>
      <SectionHeader>Approvals</SectionHeader>
      <DataTable caption="Approvals" columns={columns} rows={approvals.data} rowKey={(a) => String(a.agent_view_id)}
        emptyTitle="No approvals" emptyText="No view has this app approved." />
    </>
  );
}
