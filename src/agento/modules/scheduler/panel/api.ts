// The scheduler API contract (scheduler/src/routes.py) and its refusal messages.
// `starts_at` and a preview's `local` are naive local times in the row's zone: never parse them
// with `Date`. `next_fire_at`, `last_fire_at` and a preview's `at` are UTC (`Z`): show them with `Timestamp`.
import { apiFetch, ApiError, LIVE_REFETCH_MS, useQuery } from "@agento/api";

export interface Can { read: boolean; write: boolean; share: boolean; delete: boolean; change_owner: boolean }
export interface Member { user_id: number; username: string }
export interface Schedule {
  id: number; source: string; source_ref: string | null; title: string; prompt: string;
  agent_view_id: number | null; agent_view_code: string | null;
  owner_user_id: number | null; owner_username: string | null; owner_agent_view_id: number | null;
  rrule: string; recurrence_label: string | null; starts_at: string | null; timezone: string;
  harness: string | null; model: string | null; enabled: boolean;
  next_fire_at: string | null; last_fire_at: string | null; last_error: string | null; created_at: string | null;
  last_run_status: string | null; conversation_id: number | null; can: Can;
  /** Only on GET /schedules/<id>, and only with the share right. */
  members?: Member[] | null;
}
export type Group = "mine" | "shared" | "agent" | "user";
export interface Options {
  presets: { label: string; rrule: string }[]; harnesses: { value: string; label: string }[];
  default_timezone: string; min_interval_minutes: number;
  agent_views: { id: number; code: string; label: string; workspace_id: number }[];
}
export interface Fire { at: string; local: string }
/** One job of the schedule (`GET …/runs`, newest first). `answer`: what its last attempt wrote into
 *  the conversation; null while it runs, or when it wrote none. */
export interface Run {
  id: number; status: string; created_at: string | null; started_at: string | null; finished_at: string | null;
  answer: string | null;
}

export const BASE = "/api/scheduler";
export const scheduleUrl = (id: number | string) => `${BASE}/schedules/${id}`;

/** Every refusal code of the API and every `last_error` value, as a sentence for the user. */
const MESSAGES = {
  unknown_field: "The form sent a field the server does not know. Reload the page.",
  missing_field: "Title, prompt, agent view and recurrence are required.",
  bad_title: "The title must be 1 to 500 characters.",
  bad_prompt: "The prompt is empty or too long.",
  bad_harness: "Pick a harness from the list.",
  bad_model: "The model name is not valid (at most 128 characters).",
  bad_enabled: "Enabled must be on or off.",
  bad_start: "The start must be a local date and time.",
  bad_rule: "The recurrence is not a valid RRULE.",
  too_frequent: "The runs are too close together. Leave more minutes between them.",
  no_occurrence: "The recurrence never fires.",
  too_complex: "The recurrence is too complex to check. Make it simpler.",
  too_far_back: "The start is too far in the past. Move the start forward.",
  bad_timezone: "The time zone is not known. Use an IANA name, such as Europe/Warsaw.",
  ended: "The recurrence has ended. It does not fire again.",
  no_agent_view: "The agent view of this schedule is gone. It does not fire.",
  no_view: "You may not use that agent view.",
  view_fixed: "The agent view is set at create. To move the schedule, delete it and create it again.",
  limit: "You have reached the maximum number of schedules.",
  vetoed: "A module refused this change.",
  changed: "The schedule changed meanwhile. Reload it and try again.",
  busy: "The server is busy. Try again.",
  no_reach: "The owner cannot reach the agent view of this schedule, so it cannot run.",
  overlap: "The previous run has not ended yet.",
  too_soon: "The schedule ran a short time ago. Try again later.",
  no_user: "That user does not exist or is not active.",
  owner: "The owner already has access.",
  bad_user: "Pick a user.",
  read_only: "This schedule is read-only.",
  not_found: "The schedule was not found.",
} as const;
export const CODES = Object.keys(MESSAGES);

/** A `last_error` value as a sentence; an unknown value as is. */
export const explain = (code: string) => (MESSAGES as Record<string, string>)[code] ?? code;

/** The refusal code of a failed request, or null. */
export function codeOf(e: unknown): string | null {
  const code = e instanceof ApiError ? (e.body as { code?: unknown } | undefined)?.code : undefined;
  return typeof code === "string" ? code : null;
}

export function message(e: unknown): string {
  if (!(e instanceof ApiError)) return "The request failed.";
  const code = codeOf(e);
  return code !== null && code in MESSAGES ? explain(code) : e.message;
}

/** The list polls: the tick, a run and other users change it (UI-8). */
export const useSchedules = () => useQuery({
  queryKey: ["scheduler", "list"], refetchInterval: LIVE_REFETCH_MS,
  queryFn: ({ signal }) => apiFetch<{ groups: Record<Group, Schedule[]> }>(`${BASE}/schedules`, { signal }),
});

/** A run ends and a new one starts without the user: poll (UI-8). */
export const useRuns = (id: number) => useQuery({
  queryKey: ["scheduler", "runs", id], refetchInterval: LIVE_REFETCH_MS,
  queryFn: ({ signal }) => apiFetch<Run[]>(`${scheduleUrl(id)}/runs`, { signal }),
});

export const useOptions = () => useQuery({
  queryKey: ["scheduler", "options"],
  queryFn: ({ signal }) => apiFetch<Options>(`${BASE}/options`, { signal }),
});
