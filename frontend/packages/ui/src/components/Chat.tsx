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
export function ChatLayout({ title, actions, children, footer, overlay, viewportRef, onScroll, label = "Timeline", onFiles }: {
  title: ReactNode; actions?: ReactNode; children: ReactNode; footer?: ReactNode; overlay?: ReactNode;
  viewportRef?: Ref<HTMLDivElement>; onScroll?: (e: UIEvent<HTMLDivElement>) => void; label?: string;
  /** Files dropped anywhere on the conversation. The whole pane is the target: a drag that
   *  has to find the composer is a drag the user aims, and the history is where the eye is. */
  onFiles?: (files: File[]) => void;
}) {
  // A counter, not a boolean: dragging over a child fires `dragleave` on the parent, and a
  // boolean makes the overlay flicker on every inner element the pointer crosses.
  const [over, setOver] = useState(0);
  const drag = onFiles && {
    onDragEnter: (e: DragEvent) => { e.preventDefault(); setOver((n) => n + 1); },
    onDragOver: (e: DragEvent) => e.preventDefault(),
    onDragLeave: () => setOver((n) => Math.max(0, n - 1)),
    onDrop: (e: DragEvent) => {
      e.preventDefault(); setOver(0);
      const picked = Array.from(e.dataTransfer.files);
      if (picked.length > 0) onFiles(picked);
    },
  };
  return (
    <section className={over > 0 ? `${classes.layout} ${classes.layoutOver}` : classes.layout}
      aria-label="Conversation" {...drag}>
      {over > 0 && (
        <div className={classes.dropHint}>
          {/* A card over a scrim, not a sheet over the pane: the conversation stays readable,
              so the drag reads as "this is where it lands", not as a page that broke. */}
          <div className={classes.dropCard}>
            <Paperclip size={18} />
            <Text size="sm" fw={500}>Drop files to attach</Text>
            <Text size="xs" c="dimmed">They go with your next message.</Text>
          </div>
        </div>
      )}
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
export function ChatMessage({ author, text, time, label, actions: extra, attachments }: {
  author: "user" | "assistant" | "incoming"; text: string; time?: string | null; label?: ReactNode;
  /** More actions for this message's hover row (Regenerate on the newest answer). */
  actions?: ReactNode;
  /** The names of the files this message carried. A message may be files and nothing else. */
  attachments?: string[];
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
        {text !== "" && <Text size="sm" className={classes.pre}>{text}</Text>}
        {attachments && attachments.length > 0 && (
          <div className={classes.chips}>
            {attachments.map((name) => <FileChip key={name} name={name} />)}
          </div>
        )}
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
export interface Attachment {
  id: string; name: string; status: "uploading" | "ready" | "error"; error?: string;
  /** The picked file's size. Shown on the chip: it is what tells a user which of two files
   *  with similar names they just attached, and why one of them was refused. */
  bytes?: number;
}

/** A size a person reads at a glance: 1 KB = 1024 B, one decimal below 10 units. */
export function fileSize(bytes: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let n = bytes, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${i === 0 || n >= 10 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

/** A long name keeps its extension: `a-very-long-report-name.pdf` reads as `a-very-l….pdf`,
 *  which is what tells two attachments apart. The full name stays in the `title`. */
export function middleEllipsis(name: string, max = 24): string {
  if (name.length <= max) return name;
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 && name.length - dot <= 6 ? name.slice(dot) : "";
  return `${name.slice(0, max - ext.length - 1)}…${ext}`;
}

/** One file, as a chip. In the composer it is removable and may still be uploading; under a
 *  sent message it is just the name. */
function FileChip({ name, icon, tone, reason, size, action }: {
  name: string; icon?: ReactNode; tone?: "error"; reason?: string; size?: number; action?: ReactNode;
}) {
  return (
    <div className={tone === "error" ? `${classes.chip} ${classes.chipError}` : classes.chip}
      title={reason ? `${name} — ${reason}` : name}>
      {icon ?? <Paperclip size={12} />}
      <Text span size="xs">{middleEllipsis(name)}</Text>
      {size !== undefined && <Text span size="xs" className={classes.chipSize}>{fileSize(size)}</Text>}
      {/* The reason is TEXT, not a hover title: a file that did not arrive has to say so to
          someone who never hovers, and to a screen reader. */}
      {reason && <Text span size="xs" className={classes.chipReason}>{reason}</Text>}
      {action}
    </div>
  );
}

function AttachmentChip({ file, onRemove }: { file: Attachment; onRemove?: (id: string) => void }) {
  const failed = file.status === "error";
  return (
    <FileChip name={file.name} tone={failed ? "error" : undefined} size={file.bytes}
      reason={failed ? (file.error ?? "upload failed") : undefined}
      icon={file.status === "uploading" ? <Loader size={12} /> : <Paperclip size={12} />}
      action={onRemove && (
        <IconAction label={`Remove ${file.name}`} icon={<X size={12} />}
          onClick={() => onRemove(file.id)} />
      )} />
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
  // A file that failed holds Send back until the user removes it: sending around it would
  // silently drop a file the user believes is attached.
  const held = files.some((f) => f.status === "uploading" || f.status === "error");
  // A message with a ready file needs no text: the files ARE the message.
  const ready = canSend && !held && (value.trim() !== "" || files.some((f) => f.status === "ready"));
  const submit = (e?: FormEvent) => { e?.preventDefault(); if (ready) onSend(); };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); }
  };
  const input = useRef<HTMLInputElement>(null);
  const take = (list: FileList | null) => {
    const picked = Array.from(list ?? []);
    if (picked.length > 0) onFiles?.(picked);
  };
  const onPaste = (e: ClipboardEvent) => {
    if (onFiles && e.clipboardData.files.length > 0) { e.preventDefault(); take(e.clipboardData.files); }
  };
  const onPick = (e: ChangeEvent<HTMLInputElement>) => { take(e.currentTarget.files); e.currentTarget.value = ""; };
  return (
    <form onSubmit={submit} onPaste={onPaste}>
      <Stack gap={TIGHT}>
        {/* One rounded field holds the text and the send button, as the chat apps do: the
            textarea is `unstyled` so the pill's own border is the only box (U9). */}
        <div className={classes.pill}>
          {files.length > 0 && (
            <div className={classes.chips} aria-live="polite" aria-label="Attached files">
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
                {/* `bottom`: the chips this button fills sit directly above it. */}
                <IconAction label="Add files" tip="bottom" size="lg" icon={<Paperclip size={16} />}
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
        </div>
        {hint && <Text id="composer-hint" size="xs" c="dimmed" className={classes.disclaimer}>{hint}</Text>}
        {error && <Text size="xs" c="red" role="alert">{error}</Text>}
      </Stack>
    </form>
  );
}

