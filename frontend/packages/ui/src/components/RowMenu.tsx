// The actions of one row, behind a vertical 3-dots button (UI-7: an icon with its name as the title).
// Panel-only, like `MenuButton`: a miniapp shows no admin row actions, so there is no `.ag-*` twin.
import { Menu } from "@mantine/core";
import { EllipsisVertical } from "lucide-react";
import { IconAction } from "./IconAction";

export interface RowAction { label: string; onClick: () => void; danger?: boolean; disabled?: boolean }

export function RowMenu({ actions, label = "Actions" }: { actions: RowAction[]; label?: string }) {
  if (!actions.length) return null;
  return (
    <Menu position="bottom-end" withinPortal>
      <Menu.Target>
        <IconAction label={label} icon={<EllipsisVertical size={16} />} />
      </Menu.Target>
      <Menu.Dropdown>
        {actions.map((a) => (
          <Menu.Item key={a.label} color={a.danger ? "red" : undefined} disabled={a.disabled} onClick={a.onClick}>
            {a.label}
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}
