// A job's facts, as `GET /api/admin/jobs/<id>` answers them. Panel-only (no miniapp shows a job):
// the admin Jobs page and a module panel fetch the job and render it here, in a `SideDrawer`.
import { Stack, Table, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { CodeBlock } from "./CodeBlock";
import { SectionHeader } from "./Headers";
import { jobTone, StatusBadge } from "./StatusBadge";
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

/** "42s", "3m 05s", "1h 02m"; "—" for none. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${pad(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${pad(Math.floor(seconds / 60) % 60)}m`;
}

const when = (v: string | null) => (v ? <Timestamp value={v} /> : "—");

export function JobDetail({ job: j }: { job: JobDetailData }) {
  const facts: [string, ReactNode][] = [
    ["Status", <StatusBadge key="s" tone={jobTone(j.status)}>{j.status}</StatusBadge>], ["Type", j.type], ["Source", j.source],
    ["Agent view", j.agent_view_code ?? "—"], ["Reference", j.reference_id ?? "—"], ["Agent", j.agent_type ?? "—"],
    ["Model", j.model ?? "—"], ["Tokens in / out", `${j.input_tokens ?? 0} / ${j.output_tokens ?? 0}`],
    ["Created", when(j.created_at)], ["Started", when(j.started_at)], ["Finished", when(j.finished_at)],
    ["Duration", formatDuration(durationOf(j))], ...(j.error_class ? [["Error class", j.error_class] as [string, ReactNode]] : []),
  ];
  return (
    <Stack gap="md">
      <Table>
        <Table.Tbody>
          {facts.map(([k, v]) => <Table.Tr key={k}><Table.Th scope="row">{k}</Table.Th><Table.Td>{v}</Table.Td></Table.Tr>)}
        </Table.Tbody>
      </Table>
      {j.error_message && <><SectionHeader>Error</SectionHeader><CodeBlock code={j.error_message} /></>}
      {j.result_summary && <><SectionHeader>Result</SectionHeader><Text size="sm">{j.result_summary}</Text></>}
      <SectionHeader>Prompt</SectionHeader><CodeBlock code={j.prompt || "—"} />
      <SectionHeader>Output</SectionHeader><CodeBlock code={j.output || "—"} />
      <SectionHeader>Replay</SectionHeader>
      <Text size="sm" c="dimmed">Runs in the cron container. Use the TUI or the CLI.</Text>
      <CodeBlock code={`bin/agento replay ${j.id}`} copy />
    </Stack>
  );
}
