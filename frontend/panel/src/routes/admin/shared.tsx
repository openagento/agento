// Shared by the admin screens (plan "Admin TUI screens in the Mantine panel"). The API refuses a
// user-role session (403); the role check here only avoids showing an empty screen.
import type { ReactNode } from "react";
import { useSearchParams } from "react-router";
import { Box } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, ApiError, useMutation, useQueryClient, useSession } from "@agento/api";
import { EmptyState, type BadgeTone } from "@agento/ui";

export const message = (e: unknown) => (e instanceof ApiError ? e.message : "The request failed.");

export function AdminOnly({ children }: { children: ReactNode }) {
  const me = useSession();
  if (me?.role !== "admin") return <EmptyState title="Not available">Only an administrator opens this screen.</EmptyState>;
  return <>{children}</>;
}

export type ScopeKind = "default" | "workspace" | "agent_view";
export interface Scope { scope: ScopeKind; scopeId: number }

/** The scope lives in the URL (`?scope=&scope_id=`), so a reload or a shared link keeps it. */
export function useScope(): Scope & { ready: boolean; query: string } {
  const [params] = useSearchParams();
  const raw = params.get("scope");
  const scope: ScopeKind = raw === "workspace" || raw === "agent_view" ? raw : "default";
  const id = Number(params.get("scope_id"));
  const scopeId = scope === "default" || !Number.isSafeInteger(id) || id < 1 ? 0 : id;
  return { scope, scopeId, ready: scope === "default" || scopeId > 0, query: `scope=${scope}&scope_id=${scopeId}` };
}

/** Some writes of a batch failed; the text names each one, built only from `message()`. */
class WriteError extends Error {}

/** `PUT /api/admin/config`: one value at one scope. A repaired dependent is named in `reset`. */
export function useConfigWrite(invalidate: string) {
  const qc = useQueryClient();
  const { scope, scopeId } = useScope();
  return useMutation({
    // allSettled, not all: the list refreshes and the toggles unlock only after every write is done.
    mutationFn: async (writes: { path: string; value: string }[]) => {
      const settled = await Promise.allSettled(writes.map((w) =>
        apiFetch<{ path: string; reset: string[] }>("/api/admin/config", {
          method: "PUT", json: { path: w.path, value: w.value, scope, scope_id: scopeId },
        })));
      const failed = settled.flatMap((r, i) => r.status === "rejected" ? [`${writes[i].path}: ${message(r.reason)}`] : []);
      if (failed.length) {
        throw new WriteError(`Saved ${writes.length - failed.length} of ${writes.length}. ${failed.join("; ")}`);
      }
      return settled.map((r) => (r as PromiseFulfilledResult<{ path: string; reset: string[] }>).value);
    },
    onSuccess: (results) => {
      const reset = results.flatMap((r) => r.reset ?? []);
      notifications.show({ message: results.length === 1 ? `Saved ${results[0].path}.` : `Saved ${results.length} values.` });
      if (reset.length) notifications.show({ color: "yellow", message: `Also reset: ${reset.join(", ")}` });
    },
    onError: (e) => notifications.show({ color: "red", message: e instanceof WriteError ? e.message : message(e) }),
    onSettled: () => void qc.invalidateQueries({ queryKey: [invalidate] }),
  });
}

/** A job as the dashboard lists it; `GET /api/admin/jobs` adds the rest (JobRow). */
export interface JobHead {
  id: number; type: string; status: string; reference_id: string | null; agent_view_code: string | null;
  created_at: string | null; started_at?: string | null; finished_at: string | null;
}
export interface JobRow extends JobHead {
  source: string; agent_type: string | null; started_at: string | null;
  input_tokens: number | null; output_tokens: number | null; error_class: string | null;
}

/** Seconds from start to finish; the API sends no duration. */
export const durationOf = (j: JobHead): number | null => {
  const ms = j.started_at && j.finished_at ? Date.parse(j.finished_at) - Date.parse(j.started_at) : NaN;
  return Number.isFinite(ms) ? Math.round(ms / 1000) : null;
};

const JOB_TONE: Record<string, BadgeTone> = { TODO: "pending", RUNNING: "running", SUCCESS: "succeeded", FAILED: "failed", DEAD: "failed" };
export const jobTone = (status: string): BadgeTone => JOB_TONE[status] ?? "neutral";

/** `credential.error_msg` never reaches the browser (SEC-6): the panel names where to read it. */
export const CREDENTIAL_ERROR_HINT = "See the message with bin/agento credential:list or the admin TUI.";

/** A badge in a table cell, kept whole: auto layout would cut it to "S…". The badge itself stays
 * equal to its kit twin (UI-2); the cell keeps it whole. */
export const whole = (badge: ReactNode) => <Box miw="max-content">{badge}</Box>;
