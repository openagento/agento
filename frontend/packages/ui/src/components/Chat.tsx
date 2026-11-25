// The chat building blocks (U1-U8): Mantine components, themed by theme.ts only. The panel's
// conversation screen composes them; it may not import Mantine itself (boundaries.test.ts).
import {
  ActionIcon, Alert, Button, Code, Collapse, Group, Input, Loader, Paper, ScrollArea, Spoiler, Stack,
  Text, Textarea, Title, Tooltip, UnstyledButton,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { Bot, Brain, ChevronRight, CircleAlert, Info, Layers, Paperclip, RotateCcw, SendHorizontal, Square, Wrench, X } from "lucide-react";
import type { ChangeEvent, ClipboardEvent, DragEvent, FormEvent, KeyboardEvent, ReactNode, Ref, UIEvent } from "react";
import { useEffect, useRef, useState } from "react";
import { SOURCE_LOCALE, useLocale, useT } from "../i18n";
import { numberFormat } from "../intl";
import { fileIcon } from "./FileIcon";
import { IconAction } from "./IconAction";
import { DayTime } from "./Timestamp";
import classes from "./Chat.module.css";
import { CopyButton } from "./CopyButton";
import { jobStateLabels, type JobState } from "./JobStatusCard";
import { Markdown } from "./Markdown";
import { StatusBadge } from "./StatusBadge";
import { Timestamp } from "./Timestamp";

/** Half the theme's `xs` spacing: tool rows and message actions sit closer than `xs` (UI-1). */
const TIGHT = "calc(var(--mantine-spacing-xs) / 2)";

/** A region that takes dropped files: the WHOLE region is the target, with a card over a scrim
 *  while a drag is over it. Without `onFiles` it is a plain element. Panel only (no kit twin):
 *  a file is staged through a session route, which a miniapp does not have. */
export function FileDrop({ onFiles, hint, as: Tag = "div", className, children, ...rest }: {
  onFiles?: (files: File[]) => void; hint: string; as?: "div" | "section"; className?: string; children: ReactNode;
  "aria-label"?: string;
}) {
  // A counter, not a boolean: dragging over a child fires `dragleave` on the parent, and a
  // boolean makes the overlay flicker on every inner element the pointer crosses.
  const [over, setOver] = useState(0);
  const t = useT("ui");
  // Only a drag of FILES: a dragged link or a text selection has nothing to attach.
  const files = (e: DragEvent) => Array.from(e.dataTransfer.types).includes("Files");
  const drag = onFiles && {
    onDragEnter: (e: DragEvent) => { if (files(e)) { e.preventDefault(); setOver((n) => n + 1); } },
    onDragOver: (e: DragEvent) => { if (files(e)) e.preventDefault(); },
    onDragLeave: () => setOver((n) => Math.max(0, n - 1)),
    onDrop: (e: DragEvent) => {
      if (!files(e)) return;
      e.preventDefault(); setOver(0);
      const picked = Array.from(e.dataTransfer.files);
      if (picked.length > 0) onFiles(picked);
    },
  };
  const shown = onFiles !== undefined && over > 0;
  return (
    <Tag className={[classes.drop, shown && classes.layoutOver, className].filter(Boolean).join(" ")} {...rest} {...drag}>
      {shown && (
        <div className={classes.dropHint}>
          {/* A card over a scrim, not a sheet over the pane: the content stays readable,
              so the drag reads as "this is where it lands", not as a page that broke. */}
          <div className={classes.dropCard}>
            <Paperclip size={18} />
            <Text size="sm" fw={500}>{t("chat.drop_title")}</Text>
            <Text size="xs" c="dimmed">{hint}</Text>
          </div>
        </div>
      )}
      {children}
    </Tag>
  );
}

/** A timeline that fills the screen: a title row, one scroll region and a footer pinned at the
 *  bottom (the composer). `overlay` floats over the bottom of the region ("N new events ↓"). */
export function ChatLayout({ title, actions, children, footer, overlay, viewportRef, onScroll, label, onFiles }: {
  title: ReactNode; actions?: ReactNode; children: ReactNode; footer?: ReactNode; overlay?: ReactNode;
  viewportRef?: Ref<HTMLDivElement>; onScroll?: (e: UIEvent<HTMLDivElement>) => void; label?: string;
  /** Files dropped anywhere on the conversation. The whole pane is the target: a drag that
   *  has to find the composer is a drag the user aims, and the history is where the eye is. */
  onFiles?: (files: File[]) => void;
}) {
  const t = useT("ui");
  return (
    <FileDrop as="section" className={classes.layout} aria-label={t("chat.conversation")} onFiles={onFiles}
      hint={t("chat.drop_hint")}>
      <Group justify="space-between" wrap="nowrap" pb="xs">
        <Title order={2} size="h4" lineClamp={1}>{title}</Title>
        {actions && <Group gap="xs" wrap="nowrap">{actions}</Group>}
      </Group>
      <div className={classes.body}>
        <ScrollArea className={classes.scroll} type="auto" scrollbars="y" viewportRef={viewportRef}
          viewportProps={{ role: "region", "aria-label": label ?? t("chat.timeline"), tabIndex: 0, onScroll }}>
          <div className={classes.column}>{children}</div>
        </ScrollArea>
        {overlay && <div className={classes.overlay}>{overlay}</div>}
      </div>
      {footer ? <div className={classes.footer}><div className={classes.column}>{footer}</div></div> : <div />}
    </FileDrop>
  );
}

/** When a turn started, on its own centred line: the one above the first message dates the
 *  conversation, and the one above each later message dates that follow-up. Without it a long
 *  thread is a wall with no clock in it. */
export function ChatTime({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  return (
    <div className={classes.time}><Text size="xs" c="dimmed"><DayTime value={value} /></Text></div>
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
  const t = useT("ui");
  const actions = (
    <Group gap={TIGHT} className={classes.actions}>
      {time && <Text size="xs" c="dimmed"><Timestamp value={time} /></Text>}
      <CopyButton value={text} label={t("chat.copy_message")} />
      {extra}
    </Group>
  );
  if (author === "assistant") {
    return <div className={`${classes.message} ${classes.prose}`}><Markdown highlight>{text}</Markdown>{actions}</div>;
  }
  return (
    <div className={`${classes.message} ${author === "user" ? classes.user : ""}`}>
      {label && <Text size="xs" c="dimmed">{label}</Text>}
      {/* The files sit ABOVE the bubble, as the composer stacked them above the text line: the
          bubble is what the person wrote, the chips are what they brought with it. */}
      {attachments && attachments.length > 0 && (
        <div className={classes.chips}>
          {attachments.map((name) => <FileChip key={name} name={name} />)}
        </div>
      )}
      {text !== "" && (
        <Paper radius="lg" px="md" py="xs" className={`${classes.bubble} ${author === "incoming" ? classes.incoming : ""}`}>
          <Text size="sm" className={classes.pre}>{text}</Text>
        </Paper>
      )}
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
  const t = useT("ui");
  const icon = running ? <Loader size={14} /> : isError
    ? <CircleAlert size={14} color="var(--mantine-color-red-text)" aria-label={t("chat.tool.failed")} /> : <Wrench size={14} aria-hidden />;
  return (
    <Disclosure icon={icon} label={summary}
      trailing={duration && <Text size="xs" c="dimmed">{duration}</Text>}>
      {(input !== undefined || output !== undefined) && (
        <Stack gap={TIGHT}>
          {input !== undefined && <><Text size="xs" c="dimmed">{t("chat.tool.input")}</Text><Code block>{input}</Code></>}
          {output !== undefined && (
            <>
              <Text size="xs" c="dimmed">{t("chat.tool.output")}</Text>
              <Spoiler maxHeight={160} showLabel={t("chat.tool.show_more")} hideLabel={t("chat.tool.show_less")}><Code block>{output}</Code></Spoiler>
            </>
          )}
        </Stack>
      )}
    </Disclosure>
  );
}

/** Several tool calls in a row, folded into "Used N tools". */
export function ToolGroup({ count, running, children }: { count: number; running: boolean; children: ReactNode }) {
  const t = useT("ui");
  return (
    <Disclosure icon={running ? <Loader size={14} /> : <Layers size={14} aria-hidden />} label={t("chat.tools_used", { count })}>
      <div className={classes.group}>{children}</div>
    </Disclosure>
  );
}

/** A sub-agent's job inside its parent's turn: one line with its state, collapsed; open, its
 *  own turn (`children`, a `ChatList`). */
export function SubagentRun({ label, state, children }: { label: string; state: JobState; children?: ReactNode }) {
  const t = useT("ui");
  return (
    <Disclosure icon={state === "running" ? <Loader size={14} /> : <Bot size={14} aria-hidden />} label={label}
      trailing={<StatusBadge tone={state}>{jobStateLabels(t)[state]}</StatusBadge>}>
      {children}
    </Disclosure>
  );
}

/** The agent's reasoning, collapsed: "Thinking…" while it streams, then "Thought". */
export function Reasoning({ text, live, seconds }: { text: string; live: boolean; seconds?: number }) {
  const t = useT("ui");
  const label = live ? t("chat.thinking") : seconds ? t("chat.thought_for", { seconds }) : t("chat.thought");
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
  const t = useT("ui");
  return (
    <Alert color="red" variant="light" title={title} icon={<CircleAlert size={18} />}>
      <Stack gap="xs">
        {children && <Text size="sm">{children}</Text>}
        {details && (
          <Disclosure icon={<Info size={14} aria-hidden />} label={t("chat.error.details")}>
            <Code block>{details}</Code>
          </Disclosure>
        )}
        {onRetry && (
          <Group>
            <Button size="xs" variant="light" color="red" leftSection={<RotateCcw size={14} />} onClick={onRetry}>{t("chat.error.retry")}</Button>
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
export function fileSize(bytes: number, locale: string = SOURCE_LOCALE): string {
  const units = ["B", "KB", "MB", "GB"];
  let n = bytes, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  const digits = i === 0 || n >= 10 ? 0 : 1;
  const number = numberFormat(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits, useGrouping: false });
  return `${number.format(n)} ${units[i]}`;
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
  const locale = useLocale();
  return (
    <div className={tone === "error" ? `${classes.chip} ${classes.chipError}` : classes.chip}
      title={reason ? `${name} — ${reason}` : name}>
      {icon ?? fileIcon(name)}
      <Text span size="xs">{middleEllipsis(name)}</Text>
      {size !== undefined && <Text span size="xs" className={classes.chipSize}>{fileSize(size, locale)}</Text>}
      {/* The reason is TEXT, not a hover title: a file that did not arrive has to say so to
          someone who never hovers, and to a screen reader. */}
      {reason && <Text span size="xs" className={classes.chipReason}>{reason}</Text>}
      {action}
    </div>
  );
}

function AttachmentChip({ file, onRemove }: { file: Attachment; onRemove?: (id: string) => void }) {
  const failed = file.status === "error";
  const t = useT("ui");
  return (
    <FileChip name={file.name} tone={failed ? "error" : undefined} size={file.bytes}
      reason={failed ? (file.error ?? t("chat.upload_failed")) : undefined}
      icon={file.status === "uploading" ? <Loader size={12} /> : fileIcon(file.name)}
      action={onRemove && (
        <IconAction label={t("chat.remove_file", { name: file.name })} icon={<X size={12} />}
          onClick={() => onRemove(file.id)} />
      )} />
  );
}

/** The files a form carries, as chips. With `onRemove` each chip has its remove button. */
export function AttachmentChips({ files, onRemove }: { files: Attachment[]; onRemove?: (id: string) => void }) {
  const t = useT("ui");
  if (files.length === 0) return null;
  return (
    <div className={classes.chips} aria-live="polite" aria-label={t("chat.attached_files")}>
      {files.map((f) => <AttachmentChip key={f.id} file={f} onRemove={onRemove} />)}
    </div>
  );
}

/** The paperclip: opens the file picker and hands over what the user picked. */
export function AttachButton({ onFiles, tip }: { onFiles: (files: File[]) => void; tip?: "bottom" }) {
  const input = useRef<HTMLInputElement>(null);
  const t = useT("ui");
  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.currentTarget.files ?? []);
    e.currentTarget.value = "";
    if (picked.length > 0) onFiles(picked);
  };
  return (
    <>
      {/* The input stays in the DOM and out of sight: the button beside it is the
          real control, so the input is hidden from the a11y tree and the tab order. */}
      <input ref={input} type="file" multiple className={classes.fileInput} tabIndex={-1}
        aria-hidden="true" onChange={onPick} />
      <IconAction label={t("chat.add_files")} tip={tip} size="lg" icon={<Paperclip size={16} />}
        onClick={() => input.current?.click()} />
    </>
  );
}

/** Files as a form field: the label and the hint of every other field, the paperclip and the
 *  chips. Without `onFiles` it only shows the files (a read-only form). */
export function FilesField({ label, hint, error, files, onFiles, onRemove, empty }: {
  label: string; hint?: ReactNode; error?: ReactNode; files: Attachment[]; onFiles?: (files: File[]) => void;
  onRemove?: (id: string) => void; empty: string;
}) {
  return (
    <Input.Wrapper label={label} description={hint} error={error} inputWrapperOrder={["label", "input", "description", "error"]}>
      <div className={classes.filesField}>
        {onFiles && <AttachButton onFiles={onFiles} />}
        <AttachmentChips files={files} onRemove={onFiles ? onRemove : undefined} />
        {files.length === 0 && <Text span size="sm" c="dimmed">{empty}</Text>}
      </div>
    </Input.Wrapper>
  );
}

export function ChatComposer({ value, onChange, onSend, canSend, hint, error, placeholder,
  stoppable = false, onStop, stopping = false, attachments, onFiles, onRemoveAttachment, focusOnMount = false }: {
  value: string; onChange: (v: string) => void; onSend: () => void; canSend: boolean; hint?: ReactNode;
  error?: ReactNode; placeholder?: string; stoppable?: boolean; onStop?: () => void; stopping?: boolean;
  attachments?: Attachment[]; onFiles?: (files: File[]) => void; onRemoveAttachment?: (id: string) => void;
  /** Put the caret in the text line as soon as the composer appears: opening a conversation is
   *  asking something, so the first keystroke should land in it with no click. It is a focus
   *  call on mount, not the `autoFocus` attribute, which a11y lint bans outright. */
  focusOnMount?: boolean;
}) {
  const t = useT("ui");
  const stop = stoppable && onStop !== undefined;
  const submitLabel = stop ? t("chat.stop") : t("chat.send");
  const line = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (focusOnMount) line.current?.focus(); }, [focusOnMount]);
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
  const onPaste = (e: ClipboardEvent) => {
    if (onFiles && e.clipboardData.files.length > 0) { e.preventDefault(); onFiles(Array.from(e.clipboardData.files)); }
  };
  return (
    <form onSubmit={submit} onPaste={onPaste}>
      <Stack gap={TIGHT}>
        {/* One rounded field holds the text and the send button, as the chat apps do: the
            textarea is `unstyled` so the pill's own border is the only box (U9). */}
        <div className={classes.pill}>
          <AttachmentChips files={files} onRemove={onRemoveAttachment} />
          <div className={classes.pillRow}>
            {/* `bottom`: the chips this button fills sit directly above it. */}
            {onFiles && <AttachButton onFiles={onFiles} tip="bottom" />}
            <Textarea className={classes.pillInput} variant="unstyled" aria-label={t("chat.message")} ref={line}
              placeholder={placeholder ?? t("chat.placeholder")} autosize minRows={1} maxRows={8} value={value}
              onChange={(e) => onChange(e.currentTarget.value)} onKeyDown={onKey}
              aria-describedby={hint ? "composer-hint" : undefined} />
            <Tooltip label={submitLabel} withArrow>
              <ActionIcon type={stop ? "button" : "submit"} size="lg" radius="xl" variant="filled"
                aria-label={submitLabel} disabled={stop ? stopping : !ready}
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

