// Every artifact of the store, for an admin (plan §2.5). Only an agent creates one.
import { useNavigate } from "react-router";
import { useCan } from "@agento/api";
import { AppLink, DataTable, PageHeader, RowMenu, type Column, type RowAction } from "@agento/ui";
import { message, useArtifacts, type Artifact } from "./api";
import { useDeleteArtifact } from "./delete";

export default function ArtifactsPage() {
  const list = useArtifacts();
  const navigate = useNavigate();
  const canDelete = useCan("versioned_artifacts.delete");
  const del = useDeleteArtifact();

  const actions = (a: Artifact): RowAction[] => [
    { label: "Open", onClick: () => navigate(`/artifacts/${a.artifact_code}`) },
    ...(canDelete ? [{ label: "Delete", danger: true, onClick: () => del.open(a.artifact_code) }] : []),
  ];
  const columns: Column<Artifact>[] = [
    { id: "code", header: "Code", sortValue: (a) => a.artifact_code,
      cell: (a) => <AppLink to={`/artifacts/${a.artifact_code}`}>{a.artifact_code}</AppLink> },
    { id: "title", header: "Title", cell: (a) => a.title ?? "", sortValue: (a) => a.title ?? "" },
    { id: "current", header: "Current", cell: (a) => (a.current_version ? <code>{a.current_version}</code> : "none") },
    { id: "share", header: "Share", cell: (a) => (a.auth_enabled ? "on" : "off") },
    { id: "drafts", header: "Open drafts", cell: (a) => String(a.open_drafts.length), sortValue: (a) => a.open_drafts.length },
    { id: "actions", header: "Actions", cell: (a) => <RowMenu actions={actions(a)} /> },
  ];

  return (
    <div className="ag-stack">
      <PageHeader title="Artifacts" description="File trees that agents made and published, with their versions." />
      {del.failure && <p className="ag-field__error" role="alert">{del.failure}</p>}
      <DataTable caption="Artifacts" columns={columns} rows={list.data} rowKey={(a) => a.artifact_code}
        loading={list.isPending} error={list.error ? message(list.error) : null} onRetry={() => void list.refetch()}
        emptyTitle="No artifacts" emptyText="An agent makes an artifact with its tools." />
      {del.dialog}
    </div>
  );
}
