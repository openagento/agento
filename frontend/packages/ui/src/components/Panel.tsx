// Panel-only layout pieces, like `RowMenu`: a module panel may not import Mantine, and no miniapp
// shows these, so they have no `.ag-*` twin (UI-2). Add a twin when a miniapp needs one.
import { Accordion, Drawer, Group, Switch, Text } from "@mantine/core";
import type { ReactNode } from "react";
import css from "./Panel.module.css";

/** Grouped form sections that open and close. `value` is the open ones (controlled), so a
 *  caller can open the section that holds an error. */
export function Sections({ value, onChange, children }: {
  value: string[]; onChange: (value: string[]) => void; children: ReactNode;
}) {
  return (
    <Accordion multiple variant="separated" radius="lg" value={value} onChange={onChange} classNames={{ item: css.section }}>
      {children}
    </Accordion>
  );
}

/** One section. `summary` shows on the closed bar, so a closed section still says what it holds. */
export function Section({ value, title, description, summary, children }: {
  value: string; title: string; description?: string; summary?: ReactNode; children: ReactNode;
}) {
  return (
    <Accordion.Item value={value}>
      <Accordion.Control>
        <Group justify="space-between" wrap="nowrap" gap="md">
          <div>
            <Text fw={600}>{title}</Text>
            {description && <Text size="sm" c="dimmed">{description}</Text>}
          </div>
          {summary && <Text size="sm" c="dimmed" ta="right" className={css.summary}>{summary}</Text>}
        </Group>
      </Accordion.Control>
      <Accordion.Panel><div className="ag-stack">{children}</div></Accordion.Panel>
    </Accordion.Item>
  );
}

/** Fields side by side, each an equal share; they wrap on a narrow screen. `align="end"` lines a
 *  trailing button up with the input next to it (its label sits above the input), and the last
 *  child keeps its own width. */
export function FieldRow({ align = "start", children }: { align?: "start" | "end"; children: ReactNode }) {
  return <div className={`${css.row} ${align === "end" ? css.end : ""}`}>{children}</div>;
}

/** A page of reading width: a form line longer than this is hard to follow. */
export function PageColumn({ children }: { children: ReactNode }) {
  return <div className={`ag-stack ${css.column}`}>{children}</div>;
}

/** An on/off switch. `hideLabel` keeps the label as the accessible name only (a table cell). */
export function SwitchField({ label, description, checked, onChange, disabled, hideLabel }: {
  label: string; description?: string; checked: boolean; onChange: (checked: boolean) => void;
  disabled?: boolean; hideLabel?: boolean;
}) {
  return (
    <Switch size="md" label={hideLabel ? undefined : label} aria-label={hideLabel ? label : undefined}
      description={description} checked={checked} disabled={disabled} onChange={(e) => onChange(e.currentTarget.checked)} />
  );
}

/** A panel that slides in from the right, over the page: a quick look that keeps the page. */
export function SideDrawer({ opened, onClose, title, actions, children }: {
  opened: boolean; onClose: () => void; title: ReactNode; actions?: ReactNode; children: ReactNode;
}) {
  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="lg" closeButtonProps={{ "aria-label": "Close" }}
      title={<Group gap="xs" wrap="nowrap">{title}{actions}</Group>}>
      {children}
    </Drawer>
  );
}
