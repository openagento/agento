// The admin Home: the TUI dashboard as the StatsGrid pattern (frontend/lookbook/StatsGrid).
import type { ComponentType } from "react";
import { Group, List, Paper, SimpleGrid, Text } from "@mantine/core";
import { Activity, Boxes, Database, Tag } from "lucide-react";
import { apiFetch, LIVE_REFETCH_MS, useErrorText, useQuery } from "@agento/api";
import { AppLink, DataTable, ErrorState, LoadingState, PageHeader, SectionHeader, useDocumentTitle, useFormat, useT } from "@agento/ui";
import { credentialBadge, type Credential } from "./Credentials";
import { useJobColumns } from "./Jobs";
import type { JobHead } from "./shared";

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

export function Dashboard() {
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const { number } = useFormat();
  const jobColumns = useJobColumns();
  const dash = useQuery({ queryKey: ["admin-dashboard"], queryFn: ({ signal }) => apiFetch<DashboardData>("/api/admin/dashboard", { signal }),
    refetchInterval: LIVE_REFETCH_MS });
  useDocumentTitle(t("dashboard.title"));
  if (dash.isPending) return <LoadingState />;
  if (dash.error) return <ErrorState message={errorText(dash.error, "dashboard.load_failed")} onRetry={() => void dash.refetch()} />;
  const d = dash.data;
  return (
    <div className="ag-stack">
      <PageHeader title={t("dashboard.title")} description={t("dashboard.description")} />
      <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }}>
        <Stat title={t("dashboard.database")} value={d.db_connected ? t("dashboard.connected") : t("dashboard.not_connected")} Icon={Database} />
        <Stat title={t("dashboard.running_jobs")} value={number(d.running_jobs)} Icon={Activity} />
        <Stat title={t("dashboard.version")} value={d.version} hint={t("dashboard.python", { version: d.python_version })} Icon={Tag} />
        <Stat title={t("dashboard.modules")} value={number(d.module_count)} Icon={Boxes} />
      </SimpleGrid>
      <SectionHeader>{t("dashboard.recent_jobs")}</SectionHeader>
      <DataTable caption={t("dashboard.recent_jobs")} columns={jobColumns} rows={d.recent_jobs} rowKey={(j) => String(j.id)} emptyTitle={t("dashboard.no_jobs")} />
      <Text size="sm"><AppLink to="/admin/jobs">{t("dashboard.all_jobs")}</AppLink></Text>
      <SimpleGrid cols={{ base: 1, md: 2 }}>
        <div className="ag-stack">
          <SectionHeader>{t("dashboard.credentials")}</SectionHeader>
          {d.credentials.length ? (
            <List listStyleType="none" spacing="xs">
              {d.credentials.map((c) => (
                <List.Item key={c.id}><Group gap="xs"><code>{c.scope}</code><Text size="sm">{c.label}</Text>{credentialBadge(t, c)}</Group></List.Item>
              ))}
            </List>
          ) : <Text size="sm" c="dimmed">{t("dashboard.no_credentials")}</Text>}
        </div>
        <div className="ag-stack">
          <SectionHeader>{t("dashboard.agent_views")}</SectionHeader>
          {d.agent_views.length ? (
            <List listStyleType="none" spacing="xs">
              {d.agent_views.map((v) => <List.Item key={v.id}><code>{v.code}</code> <Text span size="sm">{v.label}</Text></List.Item>)}
            </List>
          ) : <Text size="sm" c="dimmed">{t("dashboard.no_agent_views")}</Text>}
        </div>
      </SimpleGrid>
    </div>
  );
}
