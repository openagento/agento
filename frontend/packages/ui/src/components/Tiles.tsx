// Panel-only (no `.ag-*` twin: a miniapp does not show these, UI-2): a tiles/list switch, a grid of
// tiles, and an app tile. The icon is a NAME from a fixed set (never a URL); module panels may not
// import lucide-react, so the name → icon map lives here. The miniapps panel test keeps
// APP_ICON_NAMES equal to src/agento/modules/miniapps/panel/icons.json, which routes.py reads.
import { Group, Menu, Popover, SegmentedControl, SimpleGrid, Text, UnstyledButton } from "@mantine/core";
import {
  AppWindow, BookOpen, Bot, Briefcase, Calculator, Calendar, ChartColumn, ChartLine, ClipboardList, Cloud, Database,
  FileText, Folder, Gauge, Globe, Image as ImageIcon, Inbox, Kanban, LayoutGrid, ListChecks, Mail, Map as MapIcon,
  MessageSquare, Settings, Table, Users, Wrench, type LucideIcon,
} from "lucide-react";
import { createElement, useState, type ReactNode } from "react";
import css from "./Editable.module.css";
import { IconAction } from "./IconAction";

const ICONS: Record<string, LucideIcon> = {
  "app-window": AppWindow, "chart-column": ChartColumn, "book-open": BookOpen, bot: Bot, briefcase: Briefcase,
  calculator: Calculator, calendar: Calendar, "chart-line": ChartLine, "clipboard-list": ClipboardList, cloud: Cloud,
  database: Database, "file-text": FileText, folder: Folder, gauge: Gauge, globe: Globe, image: ImageIcon, inbox: Inbox,
  kanban: Kanban, "list-checks": ListChecks, mail: Mail, map: MapIcon, "message-square": MessageSquare,
  settings: Settings, table: Table, users: Users, wrench: Wrench,
};

export const APP_ICON_NAMES = Object.keys(ICONS);

/** The icon of a name; an unknown or no name is the placeholder. */
export function AppIcon({ name, size = 16 }: { name: string | null | undefined; size?: number }) {
  return createElement((name && ICONS[name]) || LayoutGrid, { size, "aria-hidden": true });
}

/** The app icon as a button: a border on hover, a click opens the icon set; a pick calls `onChange`
 *  (null: the placeholder). */
export function IconPicker({ value, onChange, size = 24 }: {
  value: string | null; onChange: (name: string | null) => void; size?: number;
}) {
  const [opened, setOpened] = useState(false);
  return (
    <Popover opened={opened} onChange={setOpened} position="bottom-start" withArrow trapFocus>
      <Popover.Target>
        <UnstyledButton className={css.iconButton} aria-label="Change icon" aria-haspopup="dialog"
          onClick={() => setOpened((o) => !o)}><AppIcon name={value} size={size} /></UnstyledButton>
      </Popover.Target>
      <Popover.Dropdown>
        <div role="radiogroup" aria-label="Icon" className={css.iconGrid}>
          {[null, ...APP_ICON_NAMES].map((n) => (
            <IconAction key={n ?? "none"} label={n ?? "default"} role="radio" aria-checked={value === n}
              icon={<AppIcon name={n} size={20} />}
              onClick={() => { setOpened(false); if (n !== value) onChange(n); }} />
          ))}
        </div>
      </Popover.Dropdown>
    </Popover>
  );
}

export type TileMode = "tiles" | "list";

export function ViewToggle({ value, onChange }: { value: TileMode; onChange: (v: TileMode) => void }) {
  return (
    <SegmentedControl aria-label="Show as" value={value} onChange={(v) => onChange(v as TileMode)}
      data={[{ value: "tiles", label: "Tiles" }, { value: "list", label: "List" }]} />
  );
}

export function TileGrid({ label, children }: { label: string; children: ReactNode }) {
  return <SimpleGrid role="list" aria-label={label} cols={{ base: 2, sm: 3, md: 4, lg: 6 }} spacing="md">{children}</SimpleGrid>;
}

export const TILE_DISABLED = "You may view this app but not run it";

/** One app. `choices` (two or more) make the tile a menu: `onOpen` gets the picked value. */
export function AppTile({ icon, title, disabled = false, badge, onOpen, choices, actions }: {
  icon: string | null | undefined; title: string; disabled?: boolean; badge?: ReactNode;
  onOpen: (choice?: string) => void; choices?: { value: string; label: string }[]; actions?: ReactNode;
}) {
  const face = (
    <UnstyledButton className="ag-stack" aria-disabled={disabled || undefined} title={disabled ? TILE_DISABLED : undefined}
      aria-haspopup={!disabled && choices && choices.length > 1 ? "menu" : undefined} p="md" ta="center"
      onClick={disabled || (choices && choices.length > 1) ? undefined : () => onOpen(choices?.[0]?.value)}>
      <Group justify="center" c={disabled ? "dimmed" : undefined}><AppIcon name={icon} size={40} /></Group>
      <Text fw={500} c={disabled ? "dimmed" : undefined} lineClamp={2}>{title}</Text>
    </UnstyledButton>
  );
  return (
    <div role="listitem" className="ag-card">
      {!disabled && choices && choices.length > 1 ? (
        <Menu position="bottom">
          <Menu.Target>{face}</Menu.Target>
          <Menu.Dropdown>
            {choices.map((c) => <Menu.Item key={c.value} onClick={() => onOpen(c.value)}>{c.label}</Menu.Item>)}
          </Menu.Dropdown>
        </Menu>
      ) : face}
      {(badge || actions) && <Group justify="center" gap="xs">{badge}{actions}</Group>}
    </div>
  );
}
