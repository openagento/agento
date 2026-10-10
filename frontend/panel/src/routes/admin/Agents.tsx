import { Group } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useNavigate } from "react-router";
import { Copy, Settings } from "lucide-react";
import { apiFetch, useQuery } from "@agento/api";
import { DataTable, IconAction, PageHeader, StatusBadge, type BadgeTone, type Column } from "@agento/ui";
import { AdminOnly, message, whole } from "./shared";

interface Agent { id: number; code: string; label: string; workspace_code: string; ingress_count: number; build_status: string }

// workspace_build.status (modules/workspace_build/sql/001_workspace_build.sql); "none" = never built.
const BUILD_TONE: Record<string, BadgeTone> = { ready: "succeeded", building: "running", failed: "failed" };
export const buildCommand = (code: string) => `bin/agento workspace:build --agent-view ${code}`;

const copyBuild = async (a: Agent) => {
  try {
    await navigator.clipboard.writeText(buildCommand(a.code));
    notifications.show({ message: `${buildCommand(a.code)} copied. It runs in the cron container: use the TUI or the CLI.` });
  } catch { notifications.show({ color: "red", message: "Copy failed." }); }
};

const columns = (open: (a: Agent) => void): Column<Agent>[] => [
  { id: "code", header: "Code", cell: (a) => <code>{a.code}</code>, sortValue: (a) => a.code },
  { id: "label", header: "Label", cell: (a) => a.label, sortValue: (a) => a.label },
  { id: "workspace", header: "Workspace", cell: (a) => a.workspace_code, sortValue: (a) => a.workspace_code },
  { id: "ingress", header: "Ingress", cell: (a) => a.ingress_count, sortValue: (a) => a.ingress_count },
  { id: "build", header: "Build", sortValue: (a) => a.build_status,
    cell: (a) => whole(<StatusBadge tone={BUILD_TONE[a.build_status] ?? "neutral"}>{a.build_status}</StatusBadge>) },
  { id: "actions", header: "Actions", cell: (a) => (
    <Group gap="xs" wrap="nowrap">
      <IconAction label="Open config" icon={<Settings size={16} />} onClick={() => open(a)} />
      <IconAction label="Copy build command" icon={<Copy size={16} />} onClick={() => void copyBuild(a)} />
    </Group>
  ) },
];

export function Agents() {
  const navigate = useNavigate();
  const agents = useQuery({ queryKey: ["admin-agents"], queryFn: ({ signal }) => apiFetch<Agent[]>("/api/admin/agents", { signal }) });
  const open = (a: Agent) => void navigate(`/admin/config?scope=agent_view&scope_id=${a.id}`);
  return (
    <AdminOnly>
      <div className="ag-stack">
        <PageHeader title="Agents" description="Agent views, their workspace, ingress bindings and last workspace build." />
        <DataTable caption="Agent views" columns={columns(open)} rows={agents.data} rowKey={(a) => String(a.id)}
          loading={agents.isPending} error={agents.error ? message(agents.error) : null} onRetry={() => void agents.refetch()}
          emptyTitle="No agent views" />
      </div>
    </AdminOnly>
  );
}
