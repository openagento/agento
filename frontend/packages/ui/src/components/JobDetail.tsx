// A job's facts, as `GET /api/admin/jobs/<id>` answers them. Panel-only (no miniapp shows a job):
// the admin Jobs page and a module panel fetch the job and render it here, in a `SideDrawer`.
import { Stack, Table, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { useFormat, useT } from "../i18n";
import { CodeBlock } from "./CodeBlock";
import { SectionHeader } from "./Headers";
import { jobTone, StatusBadge, useJobStatusText } from "./StatusBadge";
import { Timestamp } from "./Timestamp";

export interface JobDetailData {
  id: number; type: string; status: string; source: string; reference_id: string | null; agent_view_code: string | null;
  agent_type: string | null; model: string | null; input_tokens: number | null; output_tokens: number | null;
  created_at: string | null; started_at: string | null; finished_at: string | null; error_class: string | null;
  error_message: string | null; result_summary: string | null; prompt: string | null; output: string | null;
}

/** Whole seconds from start to finish, or null while either is missing. */
export const durationOf = (j: { started_at?: string | null; finished_at?: string | null }): number | null => {
  const ms = j.started_at && j.finished_at ? Date.parse(j.finished_at) - Date.parse(j.started_at) : NaN;
  return Number.isFinite(ms) ? Math.round(ms / 1000) : null;
};

const when = (v: string | null) => (v ? <Timestamp value={v} /> : "—");

export function JobDetail({ job: j }: { job: JobDetailData }) {
  const t = useT("ui");
  const format = useFormat();
  const statusText = useJobStatusText();
  const facts: [id: string, label: string, value: ReactNode][] = [
    ["status", t("job.fact.status"), <StatusBadge key="s" tone={jobTone(j.status)}>{statusText(j.status)}</StatusBadge>],
    ["type", t("job.fact.type"), j.type], ["source", t("job.fact.source"), j.source],
    ["agent_view", t("job.fact.agent_view"), j.agent_view_code ?? "—"], ["reference", t("job.fact.reference"), j.reference_id ?? "—"],
    ["agent", t("job.fact.agent"), j.agent_type ?? "—"], ["model", t("job.fact.model"), j.model ?? "—"],
    ["tokens", t("job.fact.tokens"), `${format.number(j.input_tokens ?? 0)} / ${format.number(j.output_tokens ?? 0)}`],
    ["created", t("job.fact.created"), when(j.created_at)], ["started", t("job.fact.started"), when(j.started_at)],
    ["finished", t("job.fact.finished"), when(j.finished_at)], ["duration", t("job.fact.duration"), format.duration(durationOf(j))],
    ...(j.error_class ? [["error_class", t("job.fact.error_class"), j.error_class] as [string, string, ReactNode]] : []),
  ];
  return (
    <Stack gap="md">
      <Table>
        <Table.Tbody>
          {facts.map(([id, label, value]) => <Table.Tr key={id}><Table.Th scope="row">{label}</Table.Th><Table.Td>{value}</Table.Td></Table.Tr>)}
        </Table.Tbody>
      </Table>
      {j.error_message && <><SectionHeader>{t("job.section.error")}</SectionHeader><CodeBlock code={j.error_message} /></>}
      {j.result_summary && <><SectionHeader>{t("job.section.result")}</SectionHeader><Text size="sm">{j.result_summary}</Text></>}
      <SectionHeader>{t("job.section.prompt")}</SectionHeader><CodeBlock code={j.prompt || "—"} />
      <SectionHeader>{t("job.section.output")}</SectionHeader><CodeBlock code={j.output || "—"} />
      <SectionHeader>{t("job.section.replay")}</SectionHeader>
      <Text size="sm" c="dimmed">{t("job.replay_hint")}</Text>
      <CodeBlock code={`bin/agento replay ${j.id}`} copy />
    </Stack>
  );
}
