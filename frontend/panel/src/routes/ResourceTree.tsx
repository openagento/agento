// The checkbox tree the Roles, Tools and Skills screens share: search, expand/collapse, select all,
// and a draft the page holds as a Set of leaf values (never useTree's own checked state, which drops
// the leaves a search hides). The page gives one memoized `toData(search)`; the tree owns `search`
// and `expanded`, so a page that keys it on its scope clears both on a scope change.
import { useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, ChevronsDownUp, ChevronsUpDown, ListChecks, ListX, RotateCcw, Save, Search } from "lucide-react";
import {
  ActionIcon, Badge, Checkbox, Divider, Group, Highlight, Paper, Stack, Text, TextInput, Tree,
  getTreeExpandedState, useTree, type RenderTreeNodePayload, type TreeNodeData,
} from "@mantine/core";
import { EmptyState, IconAction } from "@agento/ui";
import { leavesOf, nodeState, toggle } from "./roleTree";

/** Every node with children, at any depth — the nodes an expand/collapse button acts on. */
export const groupValues = (data: TreeNodeData[]): string[] => data.flatMap((n) =>
  n.children?.length ? [n.value, ...groupValues(n.children)] : []);

export function ResourceTree({ toData, checked, locked, onChange, busy, renderIcon, renderInfo, emptyTitle, emptyText }: {
  toData: (search: string) => TreeNodeData[];
  checked: ReadonlySet<string>; locked: ReadonlySet<string>; onChange: (next: Set<string>) => void; busy: boolean;
  renderIcon?: (node: TreeNodeData) => ReactNode; renderInfo: (node: TreeNodeData, search: string) => ReactNode;
  emptyTitle: string; emptyText: ReactNode;
}) {
  const [search, setSearch] = useState("");
  const full = useMemo(() => toData(""), [toData]);
  // Tree re-initializes on every new `data`: it must be memoized.
  const data = useMemo(() => (search ? toData(search) : full), [toData, search, full]);
  const [expanded, setExpanded] = useState(() => getTreeExpandedState(full, "*"));
  // Merged, not replaced: a node a search hides keeps its expanded state for when it comes back.
  const tree = useTree({ expandedState: expanded, onExpandedStateChange: (s) => setExpanded((prev) => ({ ...prev, ...s })) });
  const setAll = (on: boolean) => onChange(data.reduce((s, n) => toggle(s, n, on, locked), checked as Set<string>));

  // The buttons follow the VISIBLE data: a search filters groups out. With no group `allOpen` is
  // vacuously true and `anyOpen` false, so both buttons hide — there is nothing to expand.
  const groups = useMemo(() => groupValues(data), [data]);
  const allOpen = groups.every((v) => expanded[v]);
  const anyOpen = groups.some((v) => expanded[v]);

  const renderNode = ({ node, expanded: open, hasChildren, elementProps }: RenderTreeNodePayload) => {
    const leaves = leavesOf(node);
    const state = nodeState(node, checked);
    const label = String(node.label);
    const box = (
      <Checkbox size="sm" aria-label={label} checked={state === "checked"} indeterminate={state === "indeterminate"}
        disabled={busy || leaves.every((v) => locked.has(v))}
        label={<Highlight highlight={search} fz="sm" fw={hasChildren ? 600 : 400} component="span">{label}</Highlight>}
        onChange={() => onChange(toggle(checked, node, state !== "checked", locked))}
        // The node's own click handler would move the focus from the checkbox to the tree item.
        wrapperProps={{ onClick: (e: React.MouseEvent) => e.stopPropagation() }} />
    );
    return (
      <Group gap="xs" wrap="nowrap" {...elementProps}>
        {hasChildren && (
          <ActionIcon variant="subtle" color="gray" size="sm" aria-label={`${open ? "Collapse" : "Expand"} ${label}`}
            aria-expanded={open} onClick={() => tree.toggleExpanded(node.value)}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </ActionIcon>
        )}
        {/* A leaf keeps the chevron's slot (same component and size), so its checkbox lines up under its group's. */}
        {!hasChildren && <ActionIcon component="span" variant="transparent" size="sm" aria-hidden />}
        {renderIcon?.(node)}
        {box}
        {hasChildren && (
          <Badge variant="default" size="sm" radius="sm">
            {leaves.filter((v) => checked.has(v)).length} of {leaves.length}
          </Badge>
        )}
        {renderInfo(node, search)}
      </Group>
    );
  };

  return (
    <Stack gap="sm">
      <Group gap="xs" wrap="nowrap">
        <TextInput flex={1} aria-label="Search resources" placeholder="Search" leftSection={<Search size={16} />}
          value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
        {!allOpen && <IconAction label="Expand all" icon={<ChevronsUpDown size={16} />} onClick={() => setExpanded(getTreeExpandedState(full, "*"))} />}
        {anyOpen && <IconAction label="Collapse all" icon={<ChevronsDownUp size={16} />} onClick={() => setExpanded({})} />}
        <Divider orientation="vertical" />
        <IconAction label="Select all" icon={<ListChecks size={16} />} disabled={busy} onClick={() => setAll(true)} />
        <IconAction label="Clear" icon={<ListX size={16} />} disabled={busy} onClick={() => setAll(false)} />
      </Group>
      {data.length
        ? <Tree data={data} tree={tree} expandOnClick={false} expandOnSpace={false} levelOffset="xl" renderNode={renderNode} />
        : <EmptyState title={search ? "Nothing matches" : emptyTitle}>{search ? "No row matches the search." : emptyText}</EmptyState>}
    </Stack>
  );
}

/** The sticky "N changes not saved" bar. Reset and Save are icons (RULES.md UI-7). */
export function UnsavedBar({ changes, busy, onReset, onSave }: {
  changes: number; busy: boolean; onReset: () => void; onSave: () => void;
}) {
  return (
    <Paper withBorder shadow="md" radius="md" p="sm" pos="sticky" bottom={0}>
      <Group justify="space-between">
        <Text fz="sm" fw={500}>{changes === 1 ? "1 change" : `${changes} changes`} not saved</Text>
        <Group gap="xs">
          <IconAction label="Reset" icon={<RotateCcw size={16} />} disabled={busy} onClick={onReset} />
          <IconAction label="Save" icon={<Save size={16} />} disabled={busy} onClick={onSave} />
        </Group>
      </Group>
    </Paper>
  );
}
