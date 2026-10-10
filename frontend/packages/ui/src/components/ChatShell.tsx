// The chat screen frame: threads on the left, the open thread on the right. Below the `sm`
// breakpoint the thread list moves into a Drawer behind a Burger (U9).
import { Box, Burger, Button, Drawer, Menu, NavLink, Stack, Text } from "@mantine/core";
import { useDisclosure, useMediaQuery } from "@mantine/hooks";
import { ChevronDown } from "lucide-react";
import type { ReactNode } from "react";
import classes from "./Chat.module.css";

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

export interface ThreadLink {
  id: number | string;
  title: string;
  description?: ReactNode;
  live?: boolean;
  active?: boolean;
  onSelect: () => void;
}

/** Threads in day groups ("Today", "Yesterday", …). The open one is `aria-current`. */
export function ThreadList({ groups, empty = "No conversations yet." }: {
  groups: { label: string; threads: ThreadLink[] }[]; empty?: ReactNode;
}) {
  const shown = groups.filter((g) => g.threads.length > 0);
  if (shown.length === 0) return <Text size="sm" c="dimmed">{empty}</Text>;
  return (
    <Stack gap="sm">
      {shown.map((g) => (
        <Box key={g.label}>
          <Text size="xs" c="dimmed" fw={600} px="xs" pb="calc(var(--mantine-spacing-xs) / 4)">{g.label}</Text>
          {g.threads.map((t) => (
            <NavLink key={t.id} component="button" color="gray" active={t.active} label={t.title}
              description={t.description} aria-current={t.active ? "page" : undefined} onClick={t.onSelect}
              rightSection={t.live ? <Text size="xs" c="green" aria-label="running">●</Text> : undefined} />
          ))}
        </Box>
      ))}
    </Stack>
  );
}

/** One button when there is one choice; a Menu when there are more ("New conversation" in a view). */
export function MenuButton({ label, items, onSelect, disabled }: {
  label: string; items: { value: string; label: string }[]; onSelect: (value: string) => void; disabled?: boolean;
}) {
  if (items.length <= 1) {
    return <Button fullWidth disabled={disabled || items.length === 0} onClick={() => items[0] && onSelect(items[0].value)}>{label}</Button>;
  }
  return (
    <Menu position="bottom-start" width="target">
      <Menu.Target>
        <Button fullWidth disabled={disabled} rightSection={<ChevronDown size={14} />}>{label}</Button>
      </Menu.Target>
      <Menu.Dropdown>
        {items.map((i) => <Menu.Item key={i.value} onClick={() => onSelect(i.value)}>{i.label}</Menu.Item>)}
      </Menu.Dropdown>
    </Menu>
  );
}


