// The actions of one row, behind a vertical 3-dots button (UI-7: an icon with its name as the title).
// Panel-only, like `MenuButton`: a miniapp shows no admin row actions, so there is no `.ag-*` twin.
import { Group, Menu, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { Archive, EllipsisVertical, Pencil, Pin, PinOff, RefreshCw } from "lucide-react";
import { IconAction } from "./IconAction";
import css from "./Chat.module.css";

/** Named, not a node: a module panel may not import lucide-react, so the icons live here. */
export type RowActionIcon = "rename" | "pin" | "unpin" | "archive" | "reconnect";
const ICONS: Record<RowActionIcon, ReactNode> = {
  rename: <Pencil size={14} />, pin: <Pin size={14} />, unpin: <PinOff size={14} />,
  archive: <Archive size={14} />, reconnect: <RefreshCw size={14} />,
};

export interface RowAction {
  label: string; onClick: () => void; danger?: boolean; disabled?: boolean; icon?: RowActionIcon;
}

/** Read-only rows shown above the actions, so one row (or one header) needs ONE menu. */
export interface RowDetail { label: string; value: ReactNode }

export function RowMenu({ actions, details = [], label = "Actions" }:
  { actions: RowAction[]; details?: RowDetail[]; label?: string }) {
  if (!actions.length && !details.length) return null;
  return (
    <Menu position="bottom-end" withinPortal>
      <Menu.Target>
        <IconAction label={label} icon={<EllipsisVertical size={16} />} />
      </Menu.Target>
      <Menu.Dropdown>
        {details.map((d) => (
          <Menu.Item key={d.label} component="div" className={css.menuDetail}>
            <Group justify="space-between" gap="md" wrap="nowrap">
              <Text size="sm" c="dimmed">{d.label}</Text>
              <Text size="sm">{d.value}</Text>
            </Group>
          </Menu.Item>
        ))}
        {details.length > 0 && actions.length > 0 && <Menu.Divider />}
        {actions.map((a) => (
          <Menu.Item key={a.label} color={a.danger ? "red" : undefined} disabled={a.disabled}
            leftSection={a.icon ? ICONS[a.icon] : undefined} onClick={a.onClick}>
            {a.label}
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}
