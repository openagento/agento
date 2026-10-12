// Every artifact of the store, for an admin (plan §2.5). Only an agent creates one.
import { Link } from "react-router";
import { DataTable, PageHeader, type Column } from "@agento/ui";
import { message, useArtifacts, type Artifact } from "./api";

const columns: Column<Artifact>[] = [
  { id: "code", header: "Code", sortValue: (a) => a.artifact_code,
    cell: (a) => <Link to={`/artifacts/${a.artifact_code}`}>{a.artifact_code}</Link> },
  { id: "title", header: "Title", cell: (a) => a.title ?? "", sortValue: (a) => a.title ?? "" },
  { id: "current", header: "Current", cell: (a) => (a.current_version ? <code>{a.current_version}</code> : "none") },
  { id: "share", header: "Share", cell: (a) => (a.auth_enabled ? "on" : "off") },
  { id: "drafts", header: "Open drafts", cell: (a) => String(a.open_drafts.length), sortValue: (a) => a.open_drafts.length },
];

export default function ArtifactsPage() {
  const list = useArtifacts();
  return (
    <div className="ag-stack">
      <PageHeader title="Artifacts" description="File trees that agents made and published, with their versions." />
      <DataTable caption="Artifacts" columns={columns} rows={list.data} rowKey={(a) => a.artifact_code}
        loading={list.isPending} error={list.error ? message(list.error) : null} onRetry={() => void list.refetch()}
        emptyTitle="No artifacts" emptyText="An agent makes an artifact with its tools." />
    </div>
  );
}
