// The chat screen frame: threads on the left, the open thread on the right. Below the `sm`
// breakpoint the thread list moves into a Drawer behind a Burger (U9).
import { Box, Burger, Button, Drawer, Menu, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { useDisclosure, useMediaQuery } from "@mantine/hooks";
import { ChevronDown, PinOff, Search, SquarePen, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import classes from "./Chat.module.css";
import { EditableTitle } from "./Headers";
import { IconAction } from "./IconAction";
import { RowMenu, type RowAction } from "./RowMenu";

/** `nav` gets `close` so a pick in the narrow Drawer also shuts it. It is called, never
 *  rendered as a component (UI-6). */
export function SplitView({ navLabel, nav, children }: {
  navLabel: string; nav: (close: () => void) => ReactNode; children: ReactNode;
}) {
  const narrow = useMediaQuery("(max-width: 48em)");
  const [opened, { toggle, close }] = useDisclosure(false);
  if (narrow) {
    return (
      <div className={`${classes.page} ${classes.narrow}`}>
        <Burger opened={opened} onClick={toggle} aria-label={navLabel} size="sm" />
        <Drawer opened={opened} onClose={close} title={navLabel} size="xs">{nav(close)}</Drawer>
        <div className={classes.main}>{children}</div>
      </div>
    );
  }
  return (
    <div className={`${classes.page} ${classes.split}`}>
      <nav aria-label={navLabel} className={classes.nav}>{nav(() => {})}</nav>
      <div className={classes.main}>{children}</div>
    </div>
  );
}

/** The rail's fixed head (new conversation, search, scope) above its scrolling thread list. */
export function Rail({ top, children }: { top: ReactNode; children: ReactNode }) {
  return (
    <>
      <div className={classes.railTop}>{top}</div>
      <div className={classes.railList}>{children}</div>
    </>
  );
}

/** The rail's search toggle. The icon lives here, not in the module panel (UI-4 in spirit). */
export function SearchToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <IconAction label={open ? "Close search" : "Search conversations"}
      icon={open ? <X size={16} /> : <Search size={16} />} onClick={onToggle} />
  );
}

export interface ThreadLink {
  id: number | string;
  title: string;
  /** The row's hover title: the channel and the agent view. Not a second line (U9). */
  description?: string;
  live?: boolean;
  active?: boolean;
  pinned?: boolean;
  onSelect: () => void;
  /** Given: the row offers Pin/Unpin, in its menu and as a hover icon when pinned. */
  onPin?: (pinned: boolean) => void;
  /** Given: the row offers Rename, which opens the title as a field in place. */
  onRename?: (title: string) => void;
  /** Anything else the row may do (Archive). */
  actions?: RowAction[];
}

/** One row: the title opens the thread, the rest shows on hover (UI-7). */
function ThreadRow({ thread }: { thread: ThreadLink }) {
  const [renaming, setRenaming] = useState(false);
  const actions: RowAction[] = [
    ...(thread.onRename ? [{ label: "Rename", icon: "rename" as const, onClick: () => setRenaming(true) }] : []),
    ...(thread.onPin ? [{ label: thread.pinned ? "Unpin" : "Pin", icon: thread.pinned ? ("unpin" as const) : ("pin" as const),
         onClick: () => thread.onPin!(!thread.pinned) }] : []),
    ...(thread.actions ?? []),
  ];
  return (
    <div className={`${classes.threadRow} ${thread.active ? classes.threadActive : ""}`}>
      {renaming && thread.onRename ? (
        <div className={classes.threadTitle}>
          <EditableTitle value={thread.title} label="Conversation title" maxLength={255} startEditing
            onDone={() => setRenaming(false)} onSave={thread.onRename} />
        </div>
      ) : (
        <Tooltip label={thread.description} disabled={!thread.description} withArrow openDelay={400}>
          <UnstyledButton className={classes.threadTitle} onClick={thread.onSelect}
            aria-current={thread.active ? "page" : undefined}>
            <Text size="sm" truncate>{thread.title}</Text>
          </UnstyledButton>
        </Tooltip>
      )}
      {/* While the menu is open its target keeps focus, so `:focus-within` holds the row open. */}
      <div className={classes.threadActions}>
        {thread.live && <Text size="xs" c="green" aria-label="running">●</Text>}
        {thread.pinned && thread.onPin && (
          <IconAction label="Unpin" icon={<PinOff size={14} />} onClick={() => thread.onPin!(false)} />
        )}
        <RowMenu actions={actions} label="Conversation actions" />
      </div>
    </div>
  );
}

/** Threads in groups ("Pinned", "Today", …). The open one is `aria-current`. */
export function ThreadList({ groups, empty = "No conversations yet." }: {
  groups: { label: string; threads: ThreadLink[] }[]; empty?: ReactNode;
}) {
  const shown = groups.filter((g) => g.threads.length > 0);
  if (shown.length === 0) return <Text size="sm" c="dimmed">{empty}</Text>;
  return (
    <>
      {shown.map((g) => (
        <Box key={g.label} className={classes.threadGroup}>
          <Text size="xs" c="dimmed" fw={600} className={classes.groupLabel}>{g.label}</Text>
          {g.threads.map((t) => <ThreadRow key={t.id} thread={t} />)}
        </Box>
      ))}
    </>
  );
}

/** One button when there is one choice; a Menu when there are more ("New conversation" in a view).
 *  `icon` is a name, not a node: a module panel resolves no icon package of its own (UI-4 in spirit). */
export function MenuButton({ label, items, onSelect, disabled, icon, variant }: {
  label: string; items: { value: string; label: string }[]; onSelect: (value: string) => void;
  disabled?: boolean; icon?: "new"; variant?: "filled" | "subtle";
}) {
  const node = icon === "new" ? <SquarePen size={16} /> : undefined;
  if (items.length <= 1) {
    return (
      <Button fullWidth justify={node ? "flex-start" : undefined} variant={variant} color={variant === "subtle" ? "gray" : undefined}
        leftSection={node} disabled={disabled || items.length === 0}
        onClick={() => items[0] && onSelect(items[0].value)}>{label}</Button>
    );
  }
  return (
    <Menu position="bottom-start" width="target">
      <Menu.Target>
        <Button fullWidth justify={node ? "space-between" : undefined} variant={variant}
          color={variant === "subtle" ? "gray" : undefined} leftSection={node} disabled={disabled}
          rightSection={<ChevronDown size={14} />}>{label}</Button>
      </Menu.Target>
      <Menu.Dropdown>
        {items.map((i) => <Menu.Item key={i.value} onClick={() => onSelect(i.value)}>{i.label}</Menu.Item>)}
      </Menu.Dropdown>
    </Menu>
  );
}
