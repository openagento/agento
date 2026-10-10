// The chat building blocks (U1-U8): Mantine components, themed by theme.ts only. The panel's
// conversation screen composes them; it may not import Mantine itself (boundaries.test.ts).
import {
  ActionIcon, Alert, Button, Code, Collapse, Group, Loader, Paper, Popover, ScrollArea, Spoiler, Stack,
  Text, Textarea, Title, Tooltip, UnstyledButton,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { Brain, ChevronRight, CircleAlert, Info, Layers, RotateCcw, SendHorizontal, Wrench } from "lucide-react";
import type { FormEvent, KeyboardEvent, ReactNode, Ref, UIEvent } from "react";
import classes from "./Chat.module.css";
import { CopyButton } from "./CopyButton";
import { IconAction } from "./IconAction";
import { Markdown } from "./Markdown";
import { Timestamp } from "./Timestamp";

/** Half the theme's `xs` spacing: tool rows and message actions sit closer than `xs` (UI-1). */
const TIGHT = "calc(var(--mantine-spacing-xs) / 2)";

/** A timeline that fills the screen: a title row, one scroll region and a footer pinned at the
 *  bottom (the composer). `overlay` floats over the bottom of the region ("N new events ↓"). */
export function ChatLayout({ title, actions, children, footer, overlay, viewportRef, onScroll, label = "Timeline" }: {
  title: ReactNode; actions?: ReactNode; children: ReactNode; footer?: ReactNode; overlay?: ReactNode;
  viewportRef?: Ref<HTMLDivElement>; onScroll?: (e: UIEvent<HTMLDivElement>) => void; label?: string;
}) {
  return (
    <section className={classes.layout} aria-label="Conversation">
      <Group justify="space-between" wrap="nowrap" pb="xs">
        <Title order={2} size="h4" lineClamp={1}>{title}</Title>
        {actions && <Group gap="xs" wrap="nowrap">{actions}</Group>}
      </Group>
      <div className={classes.body}>
        <ScrollArea className={classes.scroll} type="auto" viewportRef={viewportRef}
          viewportProps={{ role: "region", "aria-label": label, tabIndex: 0, onScroll }}>
          <div className={classes.column}>{children}</div>
        </ScrollArea>
        {overlay && <div className={classes.overlay}>{overlay}</div>}
      </div>
      {footer ? <div className={classes.footer}><div className={classes.column}>{footer}</div></div> : <div />}
    </section>
  );
}

/** The ordered list the timeline items go into (`aria-live` so new items are read out). */
export function ChatList({ children }: { children: ReactNode }) {
  return <ol className={classes.list} aria-live="polite">{children}</ol>;
}

/** One message. `user`: a bubble on the right, plain text. `assistant`: full width Markdown.
 *  `incoming`: what a channel sent (a trigger), plain text on the left under its `label`. The
 *  time and a copy button show on hover. */
export function ChatMessage({ author, text, time, label }: {
  author: "user" | "assistant" | "incoming"; text: string; time?: string | null; label?: ReactNode;
}) {
  const actions = (
    <Group gap={TIGHT} className={classes.actions}>
      {time && <Text size="xs" c="dimmed"><Timestamp value={time} /></Text>}
      <CopyButton value={text} label="Copy message" />
    </Group>
  );
  if (author === "assistant") {
    return <div className={classes.message}><Markdown highlight>{text}</Markdown>{actions}</div>;
  }
  return (
    <div className={`${classes.message} ${author === "user" ? classes.user : ""}`}>
      {label && <Text size="xs" c="dimmed">{label}</Text>}
      <Paper radius="lg" px="md" py="xs" className={`${classes.bubble} ${author === "incoming" ? classes.incoming : ""}`}>
        <Text size="sm" className={classes.pre}>{text}</Text>
      </Paper>
      {actions}
    </div>
  );
}

/** A one-line row that opens a detail below it. Disabled when there is nothing to open. */
function Disclosure({ icon, label, trailing, children, defaultOpen = false }: {
  icon: ReactNode; label: ReactNode; trailing?: ReactNode; children?: ReactNode; defaultOpen?: boolean;
}) {
  const [open, { toggle }] = useDisclosure(defaultOpen);
  const has = children !== undefined && children !== null && children !== false;
  return (
    <div>
      <UnstyledButton className={classes.row} onClick={toggle} disabled={!has} aria-expanded={has ? open : undefined}>
        <Group gap="xs" wrap="nowrap">
          {icon}
          <Text size="sm" truncate className={classes.grow}>{label}</Text>
          {trailing}
          {has && <ChevronRight size={14} className={`${classes.chevron} ${open ? classes.open : ""}`} aria-hidden />}
        </Group>
      </UnstyledButton>
      {has && <Collapse expanded={open}><div className={classes.detail}>{children}</div></Collapse>}
    </div>
  );
}

/** One tool call: a summary line with a spinner (running) or its duration, and the input and
 *  output (long output behind "Show more") when the reader may see them. */
export function ToolCall({ summary, running, isError = false, duration, input, output }: {
  summary: string; running: boolean; isError?: boolean; duration?: string; input?: string; output?: string;
}) {
  const icon = running ? <Loader size={14} /> : isError
    ? <CircleAlert size={14} color="var(--mantine-color-red-text)" aria-label="failed" /> : <Wrench size={14} aria-hidden />;
  return (
    <Disclosure icon={icon} label={summary}
      trailing={duration && <Text size="xs" c="dimmed">{duration}</Text>}>
      {(input !== undefined || output !== undefined) && (
        <Stack gap={TIGHT}>
          {input !== undefined && <><Text size="xs" c="dimmed">Input</Text><Code block>{input}</Code></>}
          {output !== undefined && (
            <>
              <Text size="xs" c="dimmed">Output</Text>
              <Spoiler maxHeight={160} showLabel="Show more" hideLabel="Show less"><Code block>{output}</Code></Spoiler>
            </>
          )}
        </Stack>
      )}
    </Disclosure>
  );
}

/** Several tool calls in a row, folded into "Used N tools". */
export function ToolGroup({ count, running, children }: { count: number; running: boolean; children: ReactNode }) {
  return (
    <Disclosure icon={running ? <Loader size={14} /> : <Layers size={14} aria-hidden />} label={`Used ${count} tools`}>
      <div className={classes.group}>{children}</div>
    </Disclosure>
  );
}

/** The agent's reasoning, collapsed: "Thinking…" while it streams, then "Thought". */
export function Reasoning({ text, live, seconds }: { text: string; live: boolean; seconds?: number }) {
  const label = live ? "Thinking…" : seconds ? `Thought for ${seconds} s` : "Thought";
  return (
    <Disclosure icon={live ? <Loader type="dots" size="xs" /> : <Brain size={14} aria-hidden />} label={label}>
      {text ? <Text size="sm" c="dimmed" className={classes.pre}>{text}</Text> : undefined}
    </Disclosure>
  );
}

/** The one status line under a turn: "Queued…", "Thinking…", "Running ls…". */
export function ChatStatus({ children, busy = true, action }: { children: ReactNode; busy?: boolean; action?: ReactNode }) {
  return (
    <Group gap="xs" role="status">
      {busy && <Loader type="dots" size="xs" />}
      <Text size="sm" c="dimmed">{children}</Text>
      {action}
    </Group>
  );
}

/** A turn that failed: one message, the raw detail folded, and Retry. */
export function ChatError({ title, children, details, onRetry }: {
  title: string; children?: ReactNode; details?: string; onRetry?: () => void;
}) {
  return (
    <Alert color="red" variant="light" title={title} icon={<CircleAlert size={18} />}>
      <Stack gap="xs">
        {children && <Text size="sm">{children}</Text>}
        {details && (
          <Disclosure icon={<Info size={14} aria-hidden />} label="Details">
            <Code block>{details}</Code>
          </Disclosure>
        )}
        {onRetry && (
          <Group>
            <Button size="xs" variant="light" color="red" leftSection={<RotateCcw size={14} />} onClick={onRetry}>Retry</Button>
          </Group>
        )}
      </Stack>
    </Alert>
  );
}

/** The composer: typing is always allowed; only sending waits (`canSend`). Enter sends,
 *  Shift+Enter adds a line, and a key that composes text (IME) never sends. */
export function ChatComposer({ value, onChange, onSend, canSend, hint, error }: {
  value: string; onChange: (v: string) => void; onSend: () => void; canSend: boolean; hint?: ReactNode; error?: ReactNode;
}) {
  const ready = canSend && value.trim() !== "";
  const submit = (e?: FormEvent) => { e?.preventDefault(); if (ready) onSend(); };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); }
  };
  return (
    <form onSubmit={submit}>
      <Stack gap={TIGHT}>
        <Group align="flex-end" gap="xs" wrap="nowrap">
          <Textarea className={classes.grow} aria-label="Message" placeholder="Message the agent…" autosize minRows={1}
            maxRows={8} value={value} onChange={(e) => onChange(e.currentTarget.value)} onKeyDown={onKey}
            aria-describedby={hint ? "composer-hint" : undefined} />
          <Tooltip label="Send" withArrow>
            <ActionIcon type="submit" size="input-sm" variant="filled" aria-label="Send" disabled={!ready}>
              <SendHorizontal size={16} />
            </ActionIcon>
          </Tooltip>
        </Group>
        {hint && <Text id="composer-hint" size="xs" c="dimmed">{hint}</Text>}
        {error && <Text size="xs" c="red" role="alert">{error}</Text>}
      </Stack>
    </form>
  );
}

/** Run details behind an info button: model, tokens, attempt, job. */
export function RunInfo({ rows }: { rows: { label: string; value: ReactNode }[] }) {
  return (
    <Popover width={300} position="bottom-end" withArrow shadow="md">
      <Popover.Target>
        <IconAction label="Run details" icon={<Info size={16} />} />
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap={TIGHT}>
          {rows.map((r) => (
            <Group key={r.label} justify="space-between" gap="md" wrap="nowrap">
              <Text size="sm" c="dimmed">{r.label}</Text>
              <Text size="sm">{r.value}</Text>
            </Group>
          ))}
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
