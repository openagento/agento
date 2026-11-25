import { Group } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useNavigate } from "react-router";
import { Copy, Settings } from "lucide-react";
import { apiFetch, LIVE_REFETCH_MS, useErrorText, useQuery } from "@agento/api";
import { DataTable, IconAction, PageHeader, StatusBadge, useFormat, useT, type BadgeTone, type Column, type Translate } from "@agento/ui";
import { whole } from "./shared";

interface Agent { id: number; code: string; label: string; workspace_code: string; ingress_count: number; build_status: string }

// workspace_build.status (modules/workspace_build/sql/001_workspace_build.sql); "none" = never built.
const BUILD_TONE: Record<string, BadgeTone> = { ready: "succeeded", building: "running", failed: "failed" };
const buildTexts = (t: Translate): Record<string, string> => ({
  ready: t("agents.build.ready"), building: t("agents.build.building"), failed: t("agents.build.failed"), none: t("agents.build.none"),
});
export const buildCommand = (code: string) => `bin/agento workspace:build --agent-view ${code}`;

const copyBuild = async (t: Translate, a: Agent) => {
  try {
    await navigator.clipboard.writeText(buildCommand(a.code));
    notifications.show({ message: t("agents.build_copied", { command: buildCommand(a.code) }) });
  } catch { notifications.show({ color: "red", message: t("agents.copy_failed") }); }
};

const columns = (t: Translate, number: (n: number) => string, open: (a: Agent) => void): Column<Agent>[] => [
  { id: "code", header: t("agents.column.code"), cell: (a) => <code>{a.code}</code>, sortValue: (a) => a.code },
  { id: "label", header: t("agents.column.label"), cell: (a) => a.label, sortValue: (a) => a.label },
  { id: "workspace", header: t("agents.column.workspace"), cell: (a) => a.workspace_code, sortValue: (a) => a.workspace_code },
  { id: "ingress", header: t("agents.column.ingress"), cell: (a) => number(a.ingress_count), sortValue: (a) => a.ingress_count },
  { id: "build", header: t("agents.column.build"), sortValue: (a) => a.build_status,
    cell: (a) => whole(<StatusBadge tone={BUILD_TONE[a.build_status] ?? "neutral"}>{buildTexts(t)[a.build_status] ?? a.build_status}</StatusBadge>) },
  { id: "actions", header: t("agents.column.actions"), cell: (a) => (
    <Group gap="xs" wrap="nowrap">
      <IconAction label={t("agents.open_config")} icon={<Settings size={16} />} onClick={() => open(a)} />
      <IconAction label={t("agents.copy_build")} icon={<Copy size={16} />} onClick={() => void copyBuild(t, a)} />
    </Group>
  ) },
];

export function Agents() {
  const navigate = useNavigate();
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const { number } = useFormat();
  const agents = useQuery({ queryKey: ["admin-agents"], queryFn: ({ signal }) => apiFetch<Agent[]>("/api/admin/agents", { signal }),
    refetchInterval: LIVE_REFETCH_MS });
  const open = (a: Agent) => void navigate(`/admin/config?scope=agent_view&scope_id=${a.id}`);
  return (
    <div className="ag-stack">
      <PageHeader title={t("agents.title")} description={t("agents.description")} />
      <DataTable caption={t("agents.caption")} columns={columns(t, number, open)} rows={agents.data} rowKey={(a) => String(a.id)}
        loading={agents.isPending} error={agents.error ? errorText(agents.error, "agents.load_failed") : null} onRetry={() => void agents.refetch()}
        emptyTitle={t("agents.empty")} />
    </div>
  );
}
