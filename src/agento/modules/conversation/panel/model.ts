import type { StreamEvent } from "@agento/api";
import type { JobState } from "@agento/ui";

/** `channel` is "panel" or the job source of a channel thread (read-only, admins only). */
export interface Thread {
  id: number; agent_view_id: number | null; title: string | null; status: string; created_at: string | null;
  updated_at: string | null; channel: string; external_ref: string | null; last_activity_at: string | null; live: boolean;
}

/** Without `run_details` a row carries only the first five fields (B8). */
export interface RunRow {
  execution_id: string; attempt: number; status: string; started_at: string | null; finished_at: string | null;
  job_id?: number; type?: string; harness?: string | null; provider?: string | null; credential?: string | null;
  model?: string | null;
  input_tokens?: number | null; output_tokens?: number | null;
}

/** The newest runs come first. `run_details`: the reader holds `conversation.run_details`. */
export interface ThreadDetail extends Thread {
  runs: RunRow[]; run_details?: boolean;
  /** Channel threads only: the reader holds `conversation.channel_write`. */
  channel_write?: boolean;
}

/** Events oldest first. */
export interface TimelinePage { events: StreamEvent[]; has_older: boolean; newest_id: number | null }

export interface Message {
  id: number; role: "user" | "assistant"; content: string; client_message_id: string | null;
  job_id: number | null; job_state: "pending" | "published" | "terminal" | null; created_at: string | null;
  blocked: boolean; blocked_reason: "paused" | "paused_unrecoverable" | null;
}

export const RUN_POLL_MS = 10_000;
export const IDLE_POLL_MS = 30_000;

/** A user turn whose job is not terminal: a run is in flight (or waiting to start). */
export const inFlight = (rows: Message[] | undefined) =>
  (rows ?? []).some((m) => m.role === "user" && m.job_state !== null && m.job_state !== "terminal");

/** The card state of a user turn. There is no job.succeeded event (PRD E8 §12): a
 *  terminal turn followed by an assistant message succeeded, one without did not. */
export function turnState(rows: Message[], index: number, streaming: boolean): JobState | null {
  const m = rows[index];
  if (m.role !== "user" || m.job_state === null) return null;
  if (m.blocked) return "blocked";
  if (m.job_state === "pending") return "pending";
  if (m.job_state === "published") return streaming ? "running" : "published";
  const next = rows.slice(index + 1);
  const end = next.findIndex((r) => r.role === "user");
  const answered = (end === -1 ? next : next.slice(0, end)).some((r) => r.role === "assistant");
  return answered ? "succeeded" : "failed";
}

export const DAY_GROUPS = ["Today", "Yesterday", "Previous 7 days", "Older"] as const;

/** The list group of a thread's last activity, by the reader's local calendar day. */
export function dayGroup(iso: string | null, now = new Date()): (typeof DAY_GROUPS)[number] {
  if (!iso) return "Older";
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const day = (n: number) => new Date(start.getFullYear(), start.getMonth(), start.getDate() - n).getTime();
  const t = Date.parse(iso);
  return t >= day(0) ? "Today" : t >= day(1) ? "Yesterday" : t >= day(7) ? "Previous 7 days" : "Older";
}
