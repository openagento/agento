// The chat building blocks (U1-U8): Mantine components, themed by theme.ts only. The panel's
// conversation screen composes them; it may not import Mantine itself (boundaries.test.ts).
import {
  ActionIcon, Alert, Button, Code, Collapse, Group, Loader, Paper, ScrollArea, Spoiler, Stack,
  Text, Textarea, Title, Tooltip, UnstyledButton,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { Bot, Brain, ChevronRight, CircleAlert, Info, Layers, Paperclip, RotateCcw, SendHorizontal, Square, Wrench, X } from "lucide-react";
import type { ChangeEvent, ClipboardEvent, DragEvent, FormEvent, KeyboardEvent, ReactNode, Ref, UIEvent } from "react";
import { useRef, useState } from "react";
import { IconAction } from "./IconAction";
import classes from "./Chat.module.css";
import { CopyButton } from "./CopyButton";
import { JOB_STATE_LABEL, type JobState } from "./JobStatusCard";
import { Markdown } from "./Markdown";
import { StatusBadge } from "./StatusBadge";
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
        <ScrollArea className={classes.scroll} type="auto" scrollbars="y" viewportRef={viewportRef}
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
export function ChatMessage({ author, text, time, label, actions: extra }: {
  author: "user" | "assistant" | "incoming"; text: string; time?: string | null; label?: ReactNode;
  /** More actions for this message's hover row (Regenerate on the newest answer). */
  actions?: ReactNode;
}) {
  const actions = (
    <Group gap={TIGHT} className={classes.actions}>
      {time && <Text size="xs" c="dimmed"><Timestamp value={time} /></Text>}
      <CopyButton value={text} label="Copy message" />
      {extra}
    </Group>
  );
  if (author === "assistant") {
    return <div className={`${classes.message} ${classes.prose}`}><Markdown highlight>{text}</Markdown>{actions}</div>;
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

/** A sub-agent's job inside its parent's turn: one line with its state, collapsed; open, its
 *  own turn (`children`, a `ChatList`). */
export function SubagentRun({ label, state, children }: { label: string; state: JobState; children?: ReactNode }) {
  return (
    <Disclosure icon={state === "running" ? <Loader size={14} /> : <Bot size={14} aria-hidden />} label={label}
      trailing={<StatusBadge tone={state}>{JOB_STATE_LABEL[state]}</StatusBadge>}>
      {children}
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
 *  Shift+Enter adds a line, and a key that composes text (IME) never sends.
 *
 *  `stoppable` + `onStop` turn the one submit control into Stop while a turn runs. It is
 *  deliberately NOT derived from a busy flag: busy is read off the status line and can be true
 *  with no running turn, and a Stop shown then answers 409. The page passes the same flag the
 *  server gives it. Enter still only ever sends. */
/** One file the composer is carrying. `uploading` shows a spinner; `error` says why it failed. */
export interface Attachment { id: string; name: string; status: "uploading" | "ready" | "error"; error?: string }

/** A chip per attached file. The name is middle-truncated by CSS, and the full one is the title. */
function AttachmentChip({ file, onRemove }: { file: Attachment; onRemove?: (id: string) => void }) {
  const failed = file.status === "error";
  return (
    <div className={failed ? `${classes.chip} ${classes.chipError}` : classes.chip}
      title={failed ? `${file.name} — ${file.error ?? "upload failed"}` : file.name}>
      {file.status === "uploading" ? <Loader size={12} /> : <Paperclip size={12} />}
      <Text span size="xs" className={classes.chipName}>{file.name}</Text>
      {onRemove && (
        <UnstyledButton className={classes.chipRemove} aria-label={`Remove ${file.name}`}
          onClick={() => onRemove(file.id)}><X size={12} /></UnstyledButton>
      )}
    </div>
  );
}

export function ChatComposer({ value, onChange, onSend, canSend, hint, error, placeholder = "Ask the agent…",
  stoppable = false, onStop, stopping = false, attachments, onFiles, onRemoveAttachment }: {
  value: string; onChange: (v: string) => void; onSend: () => void; canSend: boolean; hint?: ReactNode;
  error?: ReactNode; placeholder?: string; stoppable?: boolean; onStop?: () => void; stopping?: boolean;
  attachments?: Attachment[]; onFiles?: (files: File[]) => void; onRemoveAttachment?: (id: string) => void;
}) {
  const stop = stoppable && onStop !== undefined;
  const files = attachments ?? [];
  const busy = files.some((f) => f.status === "uploading");
  // A message with a ready file needs no text: the files ARE the message.
  const ready = canSend && !busy && (value.trim() !== "" || files.some((f) => f.status === "ready"));
  const submit = (e?: FormEvent) => { e?.preventDefault(); if (ready) onSend(); };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); }
  };
  const input = useRef<HTMLInputElement>(null);
  // A counter, not a boolean: dragging over a child fires `dragleave` on the parent, and a
  // boolean makes the overlay flicker on every inner element the pointer crosses.
  const [over, setOver] = useState(0);
  const take = (list: FileList | null) => {
    const picked = Array.from(list ?? []);
    if (picked.length > 0) onFiles?.(picked);
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault(); setOver(0); take(e.dataTransfer.files);
  };
  const onPaste = (e: ClipboardEvent) => {
    if (onFiles && e.clipboardData.files.length > 0) { e.preventDefault(); take(e.clipboardData.files); }
  };
  const onPick = (e: ChangeEvent<HTMLInputElement>) => { take(e.currentTarget.files); e.currentTarget.value = ""; };
  return (
    <form onSubmit={submit} onPaste={onPaste}
      onDragEnter={onFiles && ((e) => { e.preventDefault(); setOver((n) => n + 1); })}
      onDragOver={onFiles && ((e) => e.preventDefault())}
      onDragLeave={onFiles && (() => setOver((n) => Math.max(0, n - 1)))}
      onDrop={onFiles && onDrop}>
      <Stack gap={TIGHT}>
        {/* One rounded field holds the text and the send button, as the chat apps do: the
            textarea is `unstyled` so the pill's own border is the only box (U9). */}
        <div className={over > 0 ? `${classes.pill} ${classes.pillOver}` : classes.pill}>
          {files.length > 0 && (
            <div className={classes.chips}>
              {files.map((f) => <AttachmentChip key={f.id} file={f} onRemove={onRemoveAttachment} />)}
            </div>
          )}
          <div className={classes.pillRow}>
            {onFiles && (
              <>
                {/* The input stays in the DOM and out of sight: the button beside it is the
                    real control, so the input is hidden from the a11y tree and the tab order. */}
                <input ref={input} type="file" multiple className={classes.fileInput} tabIndex={-1}
                  aria-hidden="true" onChange={onPick} />
                <IconAction label="Add files" icon={<Paperclip size={16} />}
                  onClick={() => input.current?.click()} />
              </>
            )}
            <Textarea className={classes.pillInput} variant="unstyled" aria-label="Message"
              placeholder={placeholder} autosize minRows={1} maxRows={8} value={value}
              onChange={(e) => onChange(e.currentTarget.value)} onKeyDown={onKey}
              aria-describedby={hint ? "composer-hint" : undefined} />
            <Tooltip label={stop ? "Stop" : "Send"} withArrow>
              <ActionIcon type={stop ? "button" : "submit"} size="lg" radius="xl" variant="filled"
                aria-label={stop ? "Stop" : "Send"} disabled={stop ? stopping : !ready}
                onClick={stop ? onStop : undefined}>
                {stop ? <Square size={14} fill="currentColor" /> : <SendHorizontal size={16} />}
              </ActionIcon>
            </Tooltip>
          </div>
          {over > 0 && <div className={classes.dropHint}><Text size="sm">Drop files to attach</Text></div>}
        </div>
        {hint && <Text id="composer-hint" size="xs" c="dimmed" className={classes.disclaimer}>{hint}</Text>}
        {error && <Text size="xs" c="red" role="alert">{error}</Text>}
      </Stack>
    </form>
  );
}

