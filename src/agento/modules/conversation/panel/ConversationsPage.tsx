import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { apiFetch, ApiError, useMutation, useQuery, useQueryClient, useSession, type StreamEvent } from "@agento/api";
import {
  Button, ChatComposer, ChatError, ChatLayout, ChatList, ChatMessage, ChatStatus, ConnectionStatus, EmptyState, ErrorState,
  LoadingState, MenuButton, PageHeader, Reasoning, RunInfo, SelectField, SplitView, ThreadList, ToolCall, ToolGroup,
  type ThreadLink,
} from "@agento/ui";
import { DAY_GROUPS, dayGroup, inFlight, turnState, type Message, type Thread, type ThreadDetail } from "./model";
import { buildItems, turns, type Item, type Shown, type Tool, type Turn } from "./timeline";
import { duration, liveSummary, toolSummary } from "./tools";
import { fetchPage, messagesKey, useConversation } from "./useConversation";

interface AgentView { id: number; code: string; label: string }
const message = (e: unknown) => (e instanceof ApiError ? e.message : "The request failed.");

/** Within this many px of the bottom, the timeline follows new events. */
const FOLLOW_PX = 80;

const threadsUrl = (scope: string) => (scope === "mine" ? "/api/conversation/threads"
  : `/api/conversation/threads?scope=channels${scope === "channels" ? "" : `&channel=${encodeURIComponent(scope.slice("channel:".length))}`}`);

const useViews = () => useQuery({ queryKey: ["agent-views"], queryFn: ({ signal }) => apiFetch<AgentView[]>("/api/agent-views", { signal }) });

function Rail({ current, close }: { current: number | null; close: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const admin = useSession()?.role === "admin";
  // "mine", "channels" (every channel thread) or "channel:<source>".
  const [scope, setScope] = useState("mine");
  const threads = useQuery({ queryKey: ["threads", scope], queryFn: ({ signal }) => apiFetch<Thread[]>(threadsUrl(scope), { signal }) });
  // The channels an admin can filter by: the ones seen in the unfiltered channel list.
  const all = useQuery({
    queryKey: ["threads", "channels"], enabled: admin,
    queryFn: ({ signal }) => apiFetch<Thread[]>(threadsUrl("channels"), { signal }),
  });
  const seen = [...new Set((all.data ?? []).map((t) => t.channel))];
  const views = useViews();
  const viewName = new Map((views.data ?? []).map((v) => [v.id, v.label || v.code]));
  const create = useMutation({
    mutationFn: (viewId: number) => apiFetch<{ id: number }>("/api/conversation/threads", { method: "POST", json: { agent_view_id: viewId } }),
    onSuccess: (t) => { void qc.invalidateQueries({ queryKey: ["threads"] }); close(); navigate(`/conversations/${t.id}`); },
  });

  const link = (t: Thread): ThreadLink => ({
    id: t.id,
    title: t.title || `Conversation ${t.id}`,
    description: [t.channel !== "panel" ? t.channel : null, t.agent_view_id ? viewName.get(t.agent_view_id) : null,
      t.status === "archived" ? "archived" : null].filter(Boolean).join(" · ") || undefined,
    live: t.live, active: t.id === current,
    onSelect: () => { close(); navigate(`/conversations/${t.id}`); },
  });
  const list = threads.data ?? [];
  return (
    <div className="ag-stack">
      <PageHeader title="Conversations" />
      <MenuButton label="New conversation" disabled={create.isPending}
        items={(views.data ?? []).map((v) => ({ value: String(v.id), label: v.label || v.code }))}
        onSelect={(v) => create.mutate(Number(v))} />
      {create.error && <p className="ag-field__error" role="alert">{message(create.error)}</p>}
      {admin && (
        <SelectField label="Show" name="thread-scope" value={scope} onChange={setScope}
          options={[{ value: "mine", label: "Mine" }, { value: "channels", label: "All channels" },
            ...seen.map((c) => ({ value: `channel:${c}`, label: c }))]} />
      )}
      {threads.isPending ? <LoadingState /> : threads.error ? <ErrorState message={message(threads.error)} /> : (
        <ThreadList groups={DAY_GROUPS.map((label) => ({
          label, threads: list.filter((t) => dayGroup(t.last_activity_at ?? t.created_at) === label).map(link),
        }))} />
      )}
    </div>
  );
}

/** The composer: typing is always allowed; only Send waits for the agent (U8). */
export function Composer({ threadId, busy }: { threadId: number; busy: boolean }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const send = useMutation({
    mutationFn: (content: string) => apiFetch(`/api/conversation/threads/${threadId}/messages`, {
      method: "POST", json: { content, client_message_id: crypto.randomUUID() },
    }),
    // The list too: the first message gives the thread its server title (U9).
    onSuccess: () => { setText(""); void qc.invalidateQueries({ queryKey: messagesKey(threadId) }); void qc.invalidateQueries({ queryKey: ["threads"] }); },
  });
  return (
    <ChatComposer value={text} onChange={setText} onSend={() => send.mutate(text)} canSend={!busy && !send.isPending}
      hint={busy ? "The agent is answering…" : "Enter sends. Shift+Enter adds a new line."}
      error={send.error ? message(send.error) : undefined} />
  );
}

const MARKER: Record<string, string> = {
  gap: "Some live output is missing here.",
  truncated: "The live output was cut here. The final answer is complete.",
};

/** The messages no loaded event stands for: their events were pruned (retention keeps message
 *  rows longer), so they are older than the timeline and go before it. A message newer than the
 *  oldest event waits for its own event instead. */
function retained(rows: Message[], events: StreamEvent[]): Item[] {
  const shown = new Set(events.filter((e) => e.kind === "message.created" || e.kind === "assistant.message")
    .map((e) => e.payload.message_id));
  const oldest = events[0]?.created_at;
  return rows.filter((m) => !shown.has(m.id) && (!oldest || !m.created_at || Date.parse(m.created_at) <= Date.parse(oldest)))
    .map((m): Item => (m.role === "user"
      ? { type: "user", key: `message-${m.id}`, content: m.content, createdAt: m.created_at }
      : { type: "answer", key: `message-${m.id}`, content: m.content }));
}

const raw = (v: unknown) => (v === undefined ? undefined : typeof v === "string" ? v : JSON.stringify(v, null, 2));

/** A plain function, so no component type is made during a render (UI-6). */
function toolView(t: Tool) {
  return (
    <ToolCall key={t.key} summary={t.done ? toolSummary(t.name, t.input) : liveSummary(toolSummary(t.name, t.input))} running={!t.done} isError={t.isError}
      duration={duration(t.startedAt, t.endedAt)} input={raw(t.input)} output={raw(t.output)} />
  );
}

function shownView(item: Shown) {
  switch (item.type) {
    case "user":
      return <li key={item.key}><ChatMessage author="user" text={item.content} time={item.createdAt} /></li>;
    case "text":
      return <li key={item.key}><ChatMessage author="assistant" text={item.text} /></li>;
    case "answer":
      return <li key={item.key}><ChatMessage author="assistant" text={item.content} /></li>;
    case "reasoning":
      return <li key={item.key}><Reasoning text={item.text} live={item.live} /></li>;
    case "tool":
      return <li key={item.key}>{toolView(item)}</li>;
    case "tools":
      return (
        <li key={item.key}>
          <ToolGroup count={item.tools.length} running={item.tools.some((t) => !t.done)}>{item.tools.map(toolView)}</ToolGroup>
        </li>
      );
    case "marker":
      return <li key={item.key} className="ag-muted">{MARKER[item.kind]}</li>;
    case "error": // a failure no run holds (its execution is unknown); text only with run details
      return <li key={item.key}><ChatError title="The agent reported an error." details={item.text || undefined} /></li>;
  }
}

/** The incoming message a channel run answers: its source and reference; the trigger text
 *  only for a reader with run details (the server sends `prompt` to that reader alone). */
function triggerView(turn: Turn, detail: ThreadDetail) {
  const p = turn.first ?? {};
  const from = [String(p.source ?? detail.channel), p.reference_id ?? detail.external_ref].filter(Boolean).join(" · ");
  const prompt = turn.first?.prompt ?? turn.finished?.prompt;
  return (
    <li key={`${turn.key}-trigger`}>
      {typeof prompt === "string"
        ? <ChatMessage author="incoming" label={`From ${from}`} text={prompt} />
        : <p className="ag-muted">From {from}</p>}
    </li>
  );
}

function failureView(turn: Turn, details: boolean, onRetry?: () => void) {
  const f = turn.failure!;
  const attempts = turn.maxAttempts ? ` after ${turn.attempt ?? f.count} of ${turn.maxAttempts} attempts` : "";
  return (
    <li key={`${turn.key}-error`}>
      <ChatError title={f.failed ? "The agent could not answer." : "The agent reported an error."}
        details={details ? (f.count > 1 ? `${f.text}\n(${f.count} errors)` : f.text) : undefined} onRetry={onRetry}>
        {f.failed ? `The run failed${attempts}.` : undefined}
      </ChatError>
    </li>
  );
}

/** The one status line of the newest turn (U3), or null when nothing is pending. An open run
 *  speaks for itself; before it, the message row's job state does. */
function statusText(rows: Message[], turn: Turn | null): string | null {
  const last = rows.map((m) => m.role).lastIndexOf("user");
  const state = last === -1 ? null : turnState(rows, last, false);
  if (state === "blocked") {
    return rows[last].blocked_reason === "paused_unrecoverable" ? "This turn cannot resume by itself."
      : "This turn is paused and resumes by itself.";
  }
  if (state === "succeeded" || state === "failed") return null;
  if (turn?.failure?.retrying) return `Retrying (attempt ${(turn.attempt ?? 1) + 1} of ${turn.maxAttempts})…`;
  if (turn?.started && !turn.finished) {
    const tail = turn.items.at(-1);
    const open = tail?.type === "tool" ? (tail.done ? undefined : tail)
      : tail?.type === "tools" ? tail.tools.findLast((t) => !t.done) : undefined;
    if (open) return `${liveSummary(toolSummary(open.name, open.input))}…`;
    if (tail?.type === "text" && tail.live) return null;
    const of = turn.attempt && turn.attempt > 1 && turn.maxAttempts ? ` (attempt ${turn.attempt} of ${turn.maxAttempts})` : "";
    return `Thinking…${of}`;
  }
  if (state === "pending") return "Queued…";
  if (state === "published") return "Starting…";
  return null;
}

export function View({ threadId }: { threadId: number }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const admin = useSession()?.role === "admin";
  const { messages, thread, store, version, stream, loaded, loadError, retry, reconnect, resume } = useConversation(threadId);
  const box = useRef<HTMLDivElement>(null);
  /** scrollHeight before an older page was merged: the prepend keeps the visible event in place. */
  const anchor = useRef<number | null>(null);
  const [following, setFollowing] = useState(true);
  const unblock = useMutation({
    mutationFn: (id: number) => apiFetch(`/api/conversation/threads/${threadId}/messages/${id}/unblock`, { method: "POST" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: messagesKey(threadId) }),
  });
  // Retry re-sends the failed turn's text as a new message: no new route (U4).
  const resend = useMutation({
    mutationFn: (content: string) => apiFetch(`/api/conversation/threads/${threadId}/messages`, {
      method: "POST", json: { content, client_message_id: crypto.randomUUID() },
    }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: messagesKey(threadId) }); void qc.invalidateQueries({ queryKey: ["threads"] }); },
  });
  const archive = useMutation({
    mutationFn: () => apiFetch(`/api/conversation/threads/${threadId}`, { method: "DELETE" }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["threads"] }); navigate("/conversations"); },
  });
  const older = useMutation({
    mutationFn: () => fetchPage(threadId, store.oldestId()),
    onSuccess: (page) => {
      anchor.current = box.current?.scrollHeight ?? null;
      if (!store.merge("older", page.events, page.has_older)) anchor.current = null;
    },
    onError: (e) => { if (e instanceof ApiError && e.status === 409) store.merge("older", [], false); },
  });

  const ready = loaded && !messages.isPending && !messages.error;
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    if (anchor.current !== null) {
      el.scrollTop += el.scrollHeight - anchor.current;
      anchor.current = null;
    } else if (following) {
      el.scrollTop = el.scrollHeight;
    }
  }, [version, following, ready]);

  if (loadError) return <ErrorState message={message(loadError)} onRetry={retry} />;
  if (!loaded || messages.isPending) return <LoadingState />;
  if (messages.error) return <ErrorState message={message(messages.error)} onRetry={() => void messages.refetch()} />;

  const onScroll = () => {
    const el = box.current!;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_PX;
    if (near === following) return;
    if (near) resume();
    else store.follow(false);
    setFollowing(near);
  };
  const jump = () => {
    resume();
    box.current!.scrollTop = box.current!.scrollHeight;
    setFollowing(true);
  };
  const unseen = following ? 0 : store.unseen;
  const events = store.events();
  const rows: Message[] = messages.data ?? [];
  const top = turns([...retained(rows, events), ...buildItems(events)]);
  const detail = thread.data;
  const channel = detail && detail.channel !== "panel" ? detail.channel : null;
  const details = detail?.run_details ?? admin;
  const busy = inFlight(rows);
  const newest = top.findLast((i): i is Turn => i.type === "turn") ?? null;
  const status = statusText(rows, newest);
  const blocked = rows.filter((m) => m.blocked_reason === "paused_unrecoverable");
  const lastUser = rows.map((m) => m.role).lastIndexOf("user");
  // A terminal turn with no answer and no loaded failure still gets its error and Retry.
  const unanswered = !channel && lastUser !== -1 && turnState(rows, lastUser, false) === "failed" && !newest?.failure?.failed;

  const list: ReactNode[] = [];
  let asked = "";
  for (const item of top) {
    if (item.type !== "turn") {
      if (item.type === "user") asked = item.content;
      list.push(shownView(item)!);
      continue;
    }
    if (channel) list.push(triggerView(item, detail!));
    for (const i of item.items) list.push(shownView(i)!);
    if (item.failure && !item.failure.retrying) {
      const text = asked;
      const retryable = !channel && item === newest && item.failure.failed && !busy && text !== "";
      list.push(failureView(item, details, retryable ? () => resend.mutate(text) : undefined));
    }
  }
  const run = detail?.runs[0];

  return (
    <ChatLayout
      title={detail?.title || `Conversation ${threadId}`}
      actions={<>
        {stream !== "live" && <ConnectionStatus state={stream} onReconnect={reconnect} />}
        {details && run && (
          <RunInfo rows={[
            { label: "Status", value: run.status },
            { label: "Attempt", value: newest?.maxAttempts ? `${run.attempt} of ${newest.maxAttempts}` : String(run.attempt) },
            ...(run.model ? [{ label: "Model", value: run.model }] : []),
            ...(run.input_tokens != null ? [{ label: "Tokens in / out", value: `${run.input_tokens} / ${run.output_tokens ?? 0}` }] : []),
            ...(admin && run.job_id ? [{ label: "Job", value: <Link to={`/admin/jobs?job=${run.job_id}`}>{run.job_id}</Link> }] : []),
          ]} />
        )}
        {detail && !channel && detail.status !== "archived" && (
          <Button variant="subtle" onClick={() => archive.mutate()} disabled={archive.isPending}>Archive</Button>
        )}
      </>}
      viewportRef={box} onScroll={onScroll}
      overlay={unseen > 0 && <Button variant="primary" onClick={jump}>{unseen} new events ↓</Button>}
      footer={detail && (channel
        ? <p className="ag-muted">Read-only: this conversation comes from {channel}.</p>
        : <Composer threadId={threadId} busy={busy} />)}
    >
      {store.hasOlder && (
        <div className="ag-row"><Button variant="subtle" onClick={() => older.mutate()} disabled={older.isPending}>Load older</Button></div>
      )}
      {older.error && (
        <p className="ag-field__error" role="alert">
          {older.error instanceof ApiError && older.error.status === 409 ? "Older events were removed." : message(older.error)}
        </p>
      )}
      <ChatList>{list}</ChatList>
      {list.length === 0 && (
        <EmptyState title="No messages yet">{channel ? undefined : "Write the first message below."}</EmptyState>
      )}
      {unanswered && (
        <ChatError title="The agent could not answer."
          onRetry={busy ? undefined : () => resend.mutate(rows[lastUser].content)} />
      )}
      {status && (
        <ChatStatus busy={!status.startsWith("This turn")}>{status}</ChatStatus>
      )}
      {blocked.map((m) => (
        <div key={m.id} className="ag-row">
          <Button onClick={() => unblock.mutate(m.id)} disabled={unblock.isPending}>Unblock the conversation</Button>
        </div>
      ))}
      {resend.error && <p className="ag-field__error" role="alert">{message(resend.error)}</p>}
      {thread.error && <ErrorState message={message(thread.error)} onRetry={() => void thread.refetch()} />}
    </ChatLayout>
  );
}

export default function ConversationsPage() {
  const { threadId } = useParams();
  const id = threadId && /^[0-9]{1,19}$/.test(threadId) ? Number(threadId) : null;
  return (
    <SplitView navLabel="Conversations" nav={(close) => <Rail current={id} close={close} />}>
      {id === null ? <EmptyState title="Pick a conversation">Or start a new one.</EmptyState> : <View key={id} threadId={id} />}
    </SplitView>
  );
}
