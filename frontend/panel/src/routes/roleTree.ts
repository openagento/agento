// The role resource tree (plan "Panel"): pure helpers over `GET /api/admin/roles/{code}/resources`.
// The checked state is a Set of leaf values held by the page, never useTree's own checked state: that
// one drops the leaves a search hides (Mantine Tree re-initializes it on every `data` change).
import type { TreeNodeData } from "@mantine/core";

export interface OperationResource { id: string; title: string; granted: boolean; inherited: boolean; builtin: boolean }
export interface ToolResource { name: string; enabled: boolean; granted: boolean; inherited: boolean }
export interface RoleResources { operations: OperationResource[]; toolsets: { toolset: string; tools: ToolResource[] }[] }

/** What a leaf row shows beside its label: `id` dimmed, then the badges. */
export interface LeafInfo { id?: string; off?: boolean; via?: boolean; builtin?: boolean }

export type NodeState = "checked" | "indeterminate" | "none";

const opValue = (id: string) => `op:${id}`;
const toolValue = (name: string) => `tool:${name}`;
const leafOf = (value: string, label: string, info: LeafInfo): TreeNodeData => ({ value, label, nodeProps: info });

/** A group whose own name matches keeps all its leaves; otherwise only the leaves that match.
 *  Groups left empty are dropped. */
export function toTreeData(res: RoleResources, search: string): TreeNodeData[] {
  const q = search.trim().toLowerCase();
  const hit = (...texts: string[]) => !q || texts.some((t) => t.toLowerCase().includes(q));
  const group = (value: string, label: string, children: TreeNodeData[]): TreeNodeData[] =>
    children.length ? [{ value, label, children }] : [];

  const ops = hit("Operations") ? res.operations : res.operations.filter((o) => hit(o.title, o.id));
  const toolsets = res.toolsets.flatMap((ts) => {
    const tools = hit("Tools", ts.toolset) ? ts.tools : ts.tools.filter((t) => hit(t.name));
    return group(`grp:ts:${ts.toolset}`, ts.toolset, tools.map((t) =>
      leafOf(toolValue(t.name), t.name, { off: !t.enabled, via: t.inherited })));
  });
  return [
    ...group("grp:ops", "Operations", ops.map((o) =>
      leafOf(opValue(o.id), o.title, { id: o.id, via: o.inherited, builtin: o.builtin }))),
    ...group("grp:tools", "Tools", toolsets),
  ];
}

export const leavesOf = (node: TreeNodeData): string[] =>
  node.children?.length ? node.children.flatMap(leavesOf) : [node.value];

export function nodeState(node: TreeNodeData, checked: ReadonlySet<string>): NodeState {
  const leaves = leavesOf(node);
  const n = leaves.filter((v) => checked.has(v)).length;
  return n === 0 ? "none" : n === leaves.length ? "checked" : "indeterminate";
}

/** Adds or removes the unlocked leaves of `node` — of the (possibly filtered) node given, so only
 *  the visible ones. */
export function toggle(checked: ReadonlySet<string>, node: TreeNodeData, on: boolean, locked: ReadonlySet<string>): Set<string> {
  const next = new Set(checked);
  for (const v of leavesOf(node)) {
    if (locked.has(v)) continue;
    if (on) next.add(v); else next.delete(v);
  }
  return next;
}

/** Inherited from the workspace, or built in for admin: always checked. Locked whether or not a
 *  redundant row exists here too: the server keeps that row (it changes no access), so an unchecked
 *  box would come back checked after a save. */
export function lockedLeaves(res: RoleResources): Set<string> {
  return new Set([
    ...res.operations.filter((o) => o.builtin || o.inherited).map((o) => opValue(o.id)),
    ...res.toolsets.flatMap((ts) => ts.tools.filter((t) => t.inherited).map((t) => toolValue(t.name))),
  ]);
}

/** The server's state as a checked set: every row here plus the locked leaves. */
export function initialChecked(res: RoleResources): Set<string> {
  return new Set([
    ...lockedLeaves(res),
    ...res.operations.filter((o) => o.granted).map((o) => opValue(o.id)),
    ...res.toolsets.flatMap((ts) => ts.tools.filter((t) => t.granted).map((t) => toolValue(t.name))),
  ]);
}

/** The PUT body names: checked leaves minus the locked ones that have no row here; a redundant row
 *  (granted and inherited) is sent, so a save keeps it. */
export function toPayload(checked: ReadonlySet<string>, res: RoleResources): { tools: string[]; operations: string[] } {
  return {
    tools: res.toolsets.flatMap((ts) => ts.tools)
      .filter((t) => checked.has(toolValue(t.name)) && !(t.inherited && !t.granted)).map((t) => t.name),
    operations: res.operations
      .filter((o) => checked.has(opValue(o.id)) && (o.granted || !(o.builtin || o.inherited))).map((o) => o.id),
  };
}

/** How many leaves differ from the server. */
export const changeCount = (checked: ReadonlySet<string>, initial: ReadonlySet<string>) =>
  [...checked].filter((v) => !initial.has(v)).length + [...initial].filter((v) => !checked.has(v)).length;
