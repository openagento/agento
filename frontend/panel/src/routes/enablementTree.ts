// The Tools/Skills enablement tree: pure helpers over `GET /api/admin/tools` and `/api/admin/skills`.
// A leaf is the item's config path — unique across toolsets, which the name is not.
import type { TreeNodeData } from "@mantine/core";

export interface EnablementItem { name: string; path: string; enabled: boolean; explicit_here: boolean; blocked_by?: string | null }
export type Groups = { group: string; items: EnablementItem[] }[];

/** What a leaf row shows beside its label. */
export interface EnablementInfo { blockedBy?: string | null; inherited?: boolean }

/** A group whose own name matches keeps all its items; otherwise only the items that match. */
export function toTreeData(groups: Groups, search: string): TreeNodeData[] {
  const q = search.trim().toLowerCase();
  const hit = (text: string) => !q || text.toLowerCase().includes(q);
  return groups.flatMap(({ group, items }) => {
    const kept = hit(group) ? items : items.filter((i) => hit(i.name));
    if (!kept.length) return [];
    return [{
      value: `grp:${group}`, label: group,
      children: kept.map((i): TreeNodeData => ({
        value: i.path, label: i.name,
        nodeProps: { blockedBy: i.blocked_by, inherited: i.enabled && !i.explicit_here } satisfies EnablementInfo,
      })),
    }];
  });
}

const all = (groups: Groups) => groups.flatMap((g) => g.items);

/** A blocked item cannot be changed here, so its leaf is locked at the state the server reports. */
export const lockedLeaves = (groups: Groups): Set<string> =>
  new Set(all(groups).filter((i) => i.blocked_by).map((i) => i.path));

export const initialChecked = (groups: Groups): Set<string> =>
  new Set(all(groups).filter((i) => i.enabled).map((i) => i.path));

/** Only the leaves that differ from the server — exactly what `useConfigWrite` takes. */
export function toWrites(checked: ReadonlySet<string>, initial: ReadonlySet<string>): { path: string; value: "0" | "1" }[] {
  const paths = new Set([...checked, ...initial]);
  return [...paths].filter((p) => checked.has(p) !== initial.has(p))
    .map((path) => ({ path, value: checked.has(path) ? "1" as const : "0" as const }));
}
