// The Tools and Skills screens: one Card per group, a Checkbox per item. Each toggle is a
// PUT /api/admin/config of the item's own gate path at the picked scope. The marks follow the
// TUI (framework/admin/screens/_enablement.py prompt_label).
import { Badge, Checkbox, Group, Stack, Tooltip } from "@mantine/core";
import { Button, Card } from "@agento/ui";
import type { useConfigWrite } from "./shared";

export interface EnablementItem { name: string; path: string; enabled: boolean; explicit_here: boolean; blocked_by?: string | null }

export function EnablementGroup({ title, items, write }: {
  title: string; items: EnablementItem[]; write: ReturnType<typeof useConfigWrite>;
}) {
  const path = (i: EnablementItem) => i.path;
  const all = (on: boolean) => {
    const writes = items.filter((i) => !i.blocked_by && i.enabled !== on).map((i) => ({ path: path(i), value: on ? "1" : "0" }));
    if (writes.length) write.mutate(writes);
  };
  return (
    <Card title={title} actions={
      <Group gap="xs">
        <Button variant="subtle" disabled={write.isPending} onClick={() => all(true)}>Enable all</Button>
        <Button variant="subtle" disabled={write.isPending} onClick={() => all(false)}>Disable all</Button>
      </Group>
    }>
      <Stack gap="xs">
        {items.map((i) => {
          const box = (
            <Checkbox label={i.name} checked={i.enabled} disabled={Boolean(i.blocked_by) || write.isPending}
              onChange={(e) => write.mutate([{ path: path(i), value: e.currentTarget.checked ? "1" : "0" }])} />
          );
          return (
            <Group key={i.name} gap="xs">
              {/* A disabled input fires no pointer events: the Tooltip sits on a wrapper. */}
              {i.blocked_by ? <Tooltip label={`Blocked by ${i.blocked_by}`}><span>{box}</span></Tooltip> : box}
              {!i.blocked_by && i.enabled && !i.explicit_here && <Badge variant="light" color="gray" size="sm">inherited</Badge>}
            </Group>
          );
        })}
      </Stack>
    </Card>
  );
}
