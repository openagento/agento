import { ActionIcon, Menu, Tooltip } from "@mantine/core";
import type { ActionIconProps } from "@mantine/core";
import { Archive, CircleCheck, CircleStop, ExternalLink, Eye, FileText, RefreshCw, Settings, Trash2 } from "lucide-react";
import type { ComponentPropsWithRef, ReactNode } from "react";
import { useT } from "../i18n";

/** An action as an icon. `label` is its hover title and its accessible name (RULES.md UI-7).
 *  The rest props reach the button, so a Popover.Target or Menu.Target can wrap it. */
/** `tip` places the tooltip. The default opens upward, which is right for an action in a row;
 *  an action with content ABOVE it (the composer's `+`, under the file chips) passes "bottom",
 *  so the tooltip never covers what the action just produced. */
/** `size` matches a neighbour of another height: two icon buttons on one bottom-aligned row
 *  read as misaligned unless their boxes are the same (the composer's `+` beside send). */
export function IconAction({ label, icon, danger = false, tip = "top", size, ...rest }:
  Omit<ComponentPropsWithRef<"button">, "children" | "color" | "size">
  & { label: string; icon: ReactNode; danger?: boolean; tip?: "top" | "bottom"; size?: ActionIconProps["size"] }) {
  return (
    <Tooltip label={label} position={tip} withArrow>
      <ActionIcon variant="subtle" size={size} color={danger ? "red" : "gray"} aria-label={label} {...rest}>{icon}</ActionIcon>
    </Tooltip>
  );
}

export function ArchiveAction(props: Omit<ComponentPropsWithRef<"button">, "children" | "color">) {
  const t = useT("ui");
  return <IconAction label={t("action.archive")} icon={<Archive size={16} />} {...props} />;
}

export function RegenerateAction(props: Omit<ComponentPropsWithRef<"button">, "children" | "color">) {
  const t = useT("ui");
  return <IconAction label={t("action.regenerate")} icon={<RefreshCw size={16} />} {...props} />;
}

type Named = Omit<ComponentPropsWithRef<"button">, "children" | "color"> & { label?: string };

export function OpenAction({ label, ...p }: Named) {
  const t = useT("ui");
  return <IconAction label={label ?? t("action.open")} icon={<ExternalLink size={16} />} {...p} />;
}

/** A quick look, in a drawer, that keeps the page. */
export function PreviewAction({ label, ...p }: Named) {
  const t = useT("ui");
  return <IconAction label={label ?? t("action.preview")} icon={<Eye size={16} />} {...p} />;
}

/** The full record behind a row. */
export function DetailsAction({ label, ...p }: Named) {
  const t = useT("ui");
  return <IconAction label={label ?? t("action.details")} icon={<FileText size={16} />} {...p} />;
}

export function MakeCurrentAction({ label, ...p }: Named) {
  const t = useT("ui");
  return <IconAction label={label ?? t("action.make_current")} icon={<CircleCheck size={16} />} {...p} />;
}

export function EndAction({ label, ...p }: Named) {
  const t = useT("ui");
  return <IconAction danger label={label ?? t("action.end")} icon={<CircleStop size={16} />} {...p} />;
}

export function DeleteAction({ label, ...p }: Named) {
  const t = useT("ui");
  return <IconAction danger label={label ?? t("action.delete")} icon={<Trash2 size={16} />} {...p} />;
}

export function SettingsAction({ label, ...p }: Named) {
  const t = useT("ui");
  return <IconAction label={label ?? t("action.settings")} icon={<Settings size={16} />} {...p} />;
}

/** Open, as an icon. With two or more `items` (e.g. the agent views an app runs in) it opens a menu. */
export function OpenMenuAction({ label, items, onSelect, disabled }: {
  label?: string; items: { value: string; label: string }[]; onSelect: (value: string) => void; disabled?: boolean;
}) {
  const t = useT("ui");
  if (items.length <= 1) {
    return <OpenAction label={label} disabled={disabled || items.length === 0} onClick={() => items[0] && onSelect(items[0].value)} />;
  }
  return (
    <Menu position="bottom-end">
      <Menu.Target><OpenAction label={label} aria-haspopup="menu" disabled={disabled} /></Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{t("action.open_in")}</Menu.Label>
        {items.map((i) => <Menu.Item key={i.value} onClick={() => onSelect(i.value)}>{i.label}</Menu.Item>)}
      </Menu.Dropdown>
    </Menu>
  );
}
