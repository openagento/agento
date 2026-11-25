// Every artifact of the store, for an admin (plan §2.5). Only an agent creates one.
import { useNavigate } from "react-router";
import { useCan, useErrorText } from "@agento/api";
import { AppLink, DataTable, PageHeader, RowMenu, useFormat, useT, type Column, type RowAction } from "@agento/ui";
import { useArtifacts, type Artifact } from "./api";
import { useDeleteArtifact } from "./delete";

export default function ArtifactsPage() {
  const t = useT("versioned_artifacts");
  const errorText = useErrorText("versioned_artifacts");
  const { number } = useFormat();
  const list = useArtifacts();
  const navigate = useNavigate();
  const canDelete = useCan("versioned_artifacts.delete");
  const del = useDeleteArtifact();

  const actions = (a: Artifact): RowAction[] => [
    { id: "open", label: t("artifacts.action.open"), onClick: () => navigate(`/artifacts/${a.artifact_code}`) },
    ...(canDelete ? [{ id: "delete", label: t("artifacts.action.delete"), danger: true, onClick: () => del.open(a.artifact_code) }] : []),
  ];
  const columns: Column<Artifact>[] = [
    { id: "code", header: t("artifacts.column.code"), sortValue: (a) => a.artifact_code,
      cell: (a) => <AppLink to={`/artifacts/${a.artifact_code}`}>{a.artifact_code}</AppLink> },
    { id: "title", header: t("artifacts.column.title"), cell: (a) => a.title ?? "", sortValue: (a) => a.title ?? "" },
    { id: "current", header: t("artifacts.column.current"),
      cell: (a) => (a.current_version ? <code>{a.current_version}</code> : t("artifacts.current.none")) },
    { id: "share", header: t("artifacts.column.share"), cell: (a) => (a.auth_enabled ? t("artifacts.share.on") : t("artifacts.share.off")) },
    { id: "drafts", header: t("artifacts.column.drafts"), cell: (a) => number(a.open_drafts.length), sortValue: (a) => a.open_drafts.length },
    { id: "actions", header: t("artifacts.column.actions"), cell: (a) => <RowMenu actions={actions(a)} /> },
  ];

  return (
    <div className="ag-stack">
      <PageHeader title={t("artifacts.title")} description={t("artifacts.description")} />
      {del.failure && <p className="ag-field__error" role="alert">{del.failure}</p>}
      <DataTable caption={t("artifacts.title")} columns={columns} rows={list.data} rowKey={(a) => a.artifact_code}
        loading={list.isPending} error={list.error ? errorText(list.error, "artifacts.load_failed") : null} onRetry={() => void list.refetch()}
        emptyTitle={t("artifacts.empty.title")} emptyText={t("artifacts.empty.text")} />
      {del.dialog}
    </div>
  );
}
