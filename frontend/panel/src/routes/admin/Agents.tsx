import { ActionIcon, Menu } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { Link } from "react-router";
import { EllipsisVertical } from "lucide-react";
import { apiFetch, useQuery } from "@agento/api";
import { DataTable, PageHeader, StatusBadge, type BadgeTone, type Column } from "@agento/ui";
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

const COLUMNS: Column<Agent>[] = [
  { id: "code", header: "Code", cell: (a) => <code>{a.code}</code>, sortValue: (a) => a.code },
  { id: "label", header: "Label", cell: (a) => a.label, sortValue: (a) => a.label },
  { id: "workspace", header: "Workspace", cell: (a) => a.workspace_code, sortValue: (a) => a.workspace_code },
  { id: "ingress", header: "Ingress", cell: (a) => a.ingress_count, sortValue: (a) => a.ingress_count },
  { id: "build", header: "Build", sortValue: (a) => a.build_status,
    cell: (a) => whole(<StatusBadge tone={BUILD_TONE[a.build_status] ?? "neutral"}>{a.build_status}</StatusBadge>) },
  { id: "actions", header: "Actions", cell: (a) => (
    <Menu position="bottom-end">
      <Menu.Target>
        <ActionIcon variant="subtle" color="gray" aria-label={`Actions for ${a.code}`}><EllipsisVertical size={16} aria-hidden /></ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Item component={Link} to={`/admin/config?scope=agent_view&scope_id=${a.id}`}>Open config</Menu.Item>
        <Menu.Item onClick={() => void copyBuild(a)}>Copy build command</Menu.Item>
      </Menu.Dropdown>
    </Menu>
  ) },
];

export function Agents() {
  const agents = useQuery({ queryKey: ["admin-agents"], queryFn: ({ signal }) => apiFetch<Agent[]>("/api/admin/agents", { signal }) });
  return (
    <AdminOnly>
      <div className="ag-stack">
        <PageHeader title="Agents" description="Agent views, their workspace, ingress bindings and last workspace build." />
        <DataTable caption="Agent views" columns={COLUMNS} rows={agents.data} rowKey={(a) => String(a.id)}
          loading={agents.isPending} error={agents.error ? message(agents.error) : null} onRetry={() => void agents.refetch()}
          emptyTitle="No agent views" />
      </div>
    </AdminOnly>
  );
}
