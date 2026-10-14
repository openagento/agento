import { ActionIcon, Menu, Tooltip } from "@mantine/core";
import { Archive, CircleCheck, CircleStop, ExternalLink, RefreshCw, Settings, Trash2 } from "lucide-react";
import type { ComponentPropsWithRef, ReactNode } from "react";

/** An action as an icon. `label` is its hover title and its accessible name (RULES.md UI-7).
 *  The rest props reach the button, so a Popover.Target or Menu.Target can wrap it. */
export function IconAction({ label, icon, danger = false, ...rest }:
  Omit<ComponentPropsWithRef<"button">, "children" | "color"> & { label: string; icon: ReactNode; danger?: boolean }) {
  return (
    <Tooltip label={label} withArrow>
      <ActionIcon variant="subtle" color={danger ? "red" : "gray"} aria-label={label} {...rest}>{icon}</ActionIcon>
    </Tooltip>
  );
}

export function ArchiveAction(props: Omit<ComponentPropsWithRef<"button">, "children" | "color">) {
  return <IconAction label="Archive" icon={<Archive size={16} />} {...props} />;
}

export function RegenerateAction(props: Omit<ComponentPropsWithRef<"button">, "children" | "color">) {
  return <IconAction label="Regenerate" icon={<RefreshCw size={16} />} {...props} />;
}

type Named = Omit<ComponentPropsWithRef<"button">, "children" | "color"> & { label?: string };

export function OpenAction({ label = "Open", ...p }: Named) {
  return <IconAction label={label} icon={<ExternalLink size={16} />} {...p} />;
}

export function MakeCurrentAction({ label = "Make current", ...p }: Named) {
  return <IconAction label={label} icon={<CircleCheck size={16} />} {...p} />;
}

export function EndAction({ label = "End", ...p }: Named) {
  return <IconAction danger label={label} icon={<CircleStop size={16} />} {...p} />;
}

export function DeleteAction({ label = "Delete", ...p }: Named) {
  return <IconAction danger label={label} icon={<Trash2 size={16} />} {...p} />;
}

export function SettingsAction({ label = "Settings", ...p }: Named) {
  return <IconAction label={label} icon={<Settings size={16} />} {...p} />;
}

/** Open, as an icon. With two or more `items` (e.g. the agent views an app runs in) it opens a menu. */
export function OpenMenuAction({ label = "Open", items, onSelect, disabled }: {
  label?: string; items: { value: string; label: string }[]; onSelect: (value: string) => void; disabled?: boolean;
}) {
  if (items.length <= 1) {
    return <OpenAction label={label} disabled={disabled || items.length === 0} onClick={() => items[0] && onSelect(items[0].value)} />;
  }
  return (
    <Menu position="bottom-end">
      <Menu.Target><OpenAction label={label} aria-haspopup="menu" disabled={disabled} /></Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Open in</Menu.Label>
        {items.map((i) => <Menu.Item key={i.value} onClick={() => onSelect(i.value)}>{i.label}</Menu.Item>)}
      </Menu.Dropdown>
    </Menu>
  );
}
