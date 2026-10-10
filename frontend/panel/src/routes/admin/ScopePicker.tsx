import { Group, SegmentedControl, Select } from "@mantine/core";
import { useSearchParams } from "react-router";
import { apiFetch, useQuery } from "@agento/api";
import { useScope, type ScopeKind } from "./shared";

interface ScopeRow { id: number; code: string; label: string; is_active: boolean }
export interface Scopes { workspaces: ScopeRow[]; agent_views: (ScopeRow & { workspace_id: number })[] }

export const useScopes = () => useQuery({
  queryKey: ["admin-scopes"], queryFn: ({ signal }) => apiFetch<Scopes>("/api/admin/scopes", { signal }),
});

const KINDS = [
  { value: "default", label: "Default" }, { value: "workspace", label: "Workspace" }, { value: "agent_view", label: "Agent view" },
];

/** Default / Workspace / Agent view, kept in `?scope=&scope_id=`. Other params are kept. */
export function ScopePicker() {
  const { scope, scopeId } = useScope();
  const [, setParams] = useSearchParams();
  const scopes = useScopes();
  const rows = scope === "workspace" ? scopes.data?.workspaces : scope === "agent_view" ? scopes.data?.agent_views : undefined;
  const set = (kind: string, id: string | null) => setParams((p) => {
    const next = new URLSearchParams(p);
    if (kind === "default") { next.delete("scope"); next.delete("scope_id"); }
    else { next.set("scope", kind); if (id) next.set("scope_id", id); else next.delete("scope_id"); }
    return next;
  });
  return (
    <Group gap="sm" align="flex-end">
      <SegmentedControl aria-label="Scope" data={KINDS} value={scope} onChange={(v) => set(v as ScopeKind, null)} />
      {rows && (
        <Select aria-label={scope === "workspace" ? "Workspace" : "Agent view"} placeholder="Choose…" searchable
          data={rows.map((r) => ({ value: String(r.id), label: `${r.code} — ${r.label}` }))}
          value={scopeId ? String(scopeId) : null} onChange={(v) => set(scope, v)} allowDeselect={false} />
      )}
    </Group>
  );
}
