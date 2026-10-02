import type { JobState } from "@agento/ui";

export interface Thread { id: number; agent_view_id: number | null; title: string | null; status: string; updated_at: string | null }

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
