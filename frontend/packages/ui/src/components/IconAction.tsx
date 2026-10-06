import { ActionIcon, Tooltip } from "@mantine/core";
import { Archive } from "lucide-react";
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
