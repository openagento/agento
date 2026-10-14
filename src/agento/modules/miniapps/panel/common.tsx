import { apiFetch, ApiError, useQuery } from "@agento/api";
import { SelectField } from "@agento/ui";

export interface AgentView { id: number; code: string; label: string; workspace_id: number }

export const message = (e: unknown) => (e instanceof ApiError ? e.message : "The request failed.");

/** The views where the user may approve and preview an app. */
export const useViews = () => useQuery({
  queryKey: ["miniapps", "views"],
  queryFn: ({ signal }) => apiFetch<AgentView[]>("/api/miniapps/agent-views", { signal }),
});

export function ViewPicker({ views, value, onChange }: { views: AgentView[] | undefined; value: number; onChange: (v: string) => void }) {
  if ((views?.length ?? 0) < 2) return null;
  return (
    <SelectField label="Agent view" name="agent-view" value={String(value)} onChange={onChange}
      options={(views ?? []).map((v) => ({ value: String(v.id), label: v.label || v.code }))} />
  );
}
