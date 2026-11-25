// The scheduler API contract (scheduler/src/routes.py) and the texts whose key the server names.
// `starts_at` and a preview's `local` are naive local times in the row's zone: never parse them
// with `Date`. `next_fire_at`, `last_fire_at` and a preview's `at` are UTC (`Z`): show them with `Timestamp`.
import { apiFetch, LIVE_REFETCH_MS, useQuery } from "@agento/api";
import { useLookup } from "@agento/ui";

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
  /** Only on GET /schedules/<id>: the files every run gets. `bytes` is null when the file is gone. */
  attachments?: { upload_id: string; name: string; bytes: number | null }[];
}
export type Group = "mine" | "shared" | "agent" | "user";
export interface Options {
  presets: { label: string; rrule: string }[]; harnesses: { value: string; label: string }[];
  default_timezone: string; min_interval_minutes: number;
  agent_views: { id: number; code: string; label: string; workspace_id: number }[];
  /** The most files one schedule takes. */
  max_uploads: number;
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

/** A `last_error` value as a sentence; an unknown value as is. */
export function useExplain(): (code: string) => string {
  const lookup = useLookup();
  return (code) => lookup("scheduler", `error.${code}`) ?? code;
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
