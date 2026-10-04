// The admin Home: the TUI dashboard as the StatsGrid pattern (frontend/lookbook/StatsGrid).
import type { ComponentType } from "react";
import { Group, List, Paper, SimpleGrid, Text } from "@mantine/core";
import { Activity, Boxes, Database, Tag } from "lucide-react";
import { Link } from "react-router";
import { apiFetch, useQuery } from "@agento/api";
import { DataTable, ErrorState, LoadingState, PageHeader, SectionHeader } from "@agento/ui";
import { credentialBadge, type Credential } from "./Credentials";
import { jobColumns } from "./Jobs";
import { message, type JobHead } from "./shared";

interface DashboardData {
  db_connected: boolean; running_jobs: number; version: string; python_version: string; module_count: number;
  recent_jobs: JobHead[];
  credentials: Pick<Credential, "id" | "scope" | "label" | "status" | "enabled">[];
  agent_views: { id: number; code: string; label: string; workspace_id: number }[];
}

function Stat({ title, value, hint, Icon }: { title: string; value: string; hint?: string; Icon: ComponentType<{ size?: number; strokeWidth?: number }> }) {
  return (
    <Paper withBorder p="md" radius="md">
      <Group justify="space-between">
        <Text size="xs" c="dimmed" tt="uppercase" fw={700}>{title}</Text>
        <Icon size={22} strokeWidth={1.5} aria-hidden />
      </Group>
      <Text fz="xl" fw={700} mt="sm">{value}</Text>
      {hint && <Text fz="xs" c="dimmed" mt={4}>{hint}</Text>}
    </Paper>
  );
}

const RECENT = jobColumns();

export function Dashboard() {
  const dash = useQuery({ queryKey: ["admin-dashboard"], queryFn: ({ signal }) => apiFetch<DashboardData>("/api/admin/dashboard", { signal }) });
  if (dash.isPending) return <LoadingState />;
  if (dash.error) return <ErrorState message={message(dash.error)} onRetry={() => void dash.refetch()} />;
  const d = dash.data;
  return (
    <div className="ag-stack">
      <PageHeader title="Dashboard" description="The state of this Agento install." />
      <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }}>
        <Stat title="Database" value={d.db_connected ? "Connected" : "Not connected"} Icon={Database} />
        <Stat title="Running jobs" value={String(d.running_jobs)} Icon={Activity} />
        <Stat title="Version" value={d.version} hint={`Python ${d.python_version}`} Icon={Tag} />
        <Stat title="Modules" value={String(d.module_count)} Icon={Boxes} />
      </SimpleGrid>
      <SectionHeader>Recent jobs</SectionHeader>
      <DataTable caption="Recent jobs" columns={RECENT} rows={d.recent_jobs} rowKey={(j) => String(j.id)} emptyTitle="No jobs yet" />
      <Text size="sm"><Link to="/admin/jobs">All jobs</Link></Text>
      <SimpleGrid cols={{ base: 1, md: 2 }}>
        <div className="ag-stack">
          <SectionHeader>Credentials</SectionHeader>
          {d.credentials.length ? (
            <List listStyleType="none" spacing="xs">
              {d.credentials.map((c) => (
                <List.Item key={c.id}><Group gap="xs"><code>{c.scope}</code><Text size="sm">{c.label}</Text>{credentialBadge(c)}</Group></List.Item>
              ))}
            </List>
          ) : <Text size="sm" c="dimmed">No credentials.</Text>}
        </div>
        <div className="ag-stack">
          <SectionHeader>Agent views</SectionHeader>
          {d.agent_views.length ? (
            <List listStyleType="none" spacing="xs">
              {d.agent_views.map((v) => <List.Item key={v.id}><code>{v.code}</code> <Text span size="sm">{v.label}</Text></List.Item>)}
            </List>
          ) : <Text size="sm" c="dimmed">No agent views.</Text>}
        </div>
      </SimpleGrid>
    </div>
  );
}
