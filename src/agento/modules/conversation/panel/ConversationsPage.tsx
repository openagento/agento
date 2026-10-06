import { useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { apiFetch, ApiError, useMutation, useQuery, useQueryClient, useSession, type StreamEvent } from "@agento/api";
import {
  Button, ConnectionStatus, EmptyState, ErrorState, JobStatusCard, JsonViewer, LoadingState, Markdown, PageHeader,
  SelectField, StatusBadge, Timestamp, type BadgeTone,
} from "@agento/ui";
import { inFlight, turnState, type Message, type RunRow, type Thread } from "./model";
import { buildItems, type Item } from "./timeline";
import { fetchPage, messagesKey, useConversation } from "./useConversation";

interface AgentView { id: number; code: string; label: string }
const message = (e: unknown) => (e instanceof ApiError ? e.message : "The request failed.");

/** Within this many px of the bottom, the timeline follows new events. */
const FOLLOW_PX = 80;

const tone = (status: string | undefined): BadgeTone =>
  status === "running" ? "running" : status === "succeeded" ? "succeeded"
    : status === "failed" || status === "dead" ? "failed" : "neutral";

const threadsUrl = (scope: string) => (scope === "mine" ? "/api/conversation/threads"
  : `/api/conversation/threads?scope=channels${scope === "channels" ? "" : `&channel=${encodeURIComponent(scope.slice("channel:".length))}`}`);

function Rail({ current }: { current: number | null }) {
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
  const views = useQuery({ queryKey: ["agent-views"], queryFn: ({ signal }) => apiFetch<AgentView[]>("/api/agent-views", { signal }) });
  const [view, setView] = useState("");
  const viewId = Number(view || views.data?.[0]?.id || 0);
  const create = useMutation({
    mutationFn: () => apiFetch<{ id: number }>("/api/conversation/threads", { method: "POST", json: { agent_view_id: viewId } }),
    onSuccess: (t) => { void qc.invalidateQueries({ queryKey: ["threads"] }); navigate(`/conversations/${t.id}`); },
  });
  const archive = useMutation({
    mutationFn: (id: number) => apiFetch(`/api/conversation/threads/${id}`, { method: "DELETE" }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["threads"] }); navigate("/conversations"); },
  });

  return (
    <nav className="ag-stack" aria-label="Conversations">
      {admin && (
        <SelectField label="Show" name="thread-scope" value={scope} onChange={setScope}
          options={[{ value: "mine", label: "Mine" }, { value: "channels", label: "All channels" },
            ...seen.map((c) => ({ value: `channel:${c}`, label: c }))]} />
      )}
      {(views.data?.length ?? 0) > 1 && (
        <SelectField label="Agent view" name="thread-view" value={String(viewId)} onChange={setView}
          options={(views.data ?? []).map((v) => ({ value: String(v.id), label: v.label || v.code }))} />
      )}
      <Button variant="primary" disabled={!viewId || create.isPending} onClick={() => create.mutate()}>New conversation</Button>
      {create.error && <p className="ag-field__error" role="alert">{message(create.error)}</p>}
      {threads.isPending ? <LoadingState /> : threads.error ? <ErrorState message={message(threads.error)} /> : (
        <ul className="ag-list">
          {(threads.data ?? []).map((t) => (
            <li key={t.id} className="ag-row">
              <Button variant={t.id === current ? "primary" : "subtle"} aria-current={t.id === current ? "page" : undefined}
                onClick={() => navigate(`/conversations/${t.id}`)}>
                {t.title || `Conversation ${t.id}`}
              </Button>
              <StatusBadge tone="neutral">{t.channel}</StatusBadge>
              {t.live && <StatusBadge tone="running">live</StatusBadge>}
              {t.status === "archived" && <span className="ag-muted">archived</span>}
              {t.id === current && t.status !== "archived" && t.channel === "panel" && (
                <Button variant="subtle" onClick={() => archive.mutate(t.id)} disabled={archive.isPending}>Archive</Button>
              )}
            </li>
          ))}
          {threads.data?.length === 0 && <li><EmptyState title="No conversations yet" /></li>}
        </ul>
      )}
    </nav>
  );
}

export function Composer({ threadId, busy }: { threadId: number; busy: boolean }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const send = useMutation({
    mutationFn: (content: string) => apiFetch(`/api/conversation/threads/${threadId}/messages`, {
      method: "POST", json: { content, client_message_id: crypto.randomUUID() },
    }),
    onSuccess: () => { setText(""); void qc.invalidateQueries({ queryKey: messagesKey(threadId) }); },
  });
  const submit = () => { if (text.trim() && !send.isPending && !busy) send.mutate(text); };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends; Shift+Enter is a new line.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); }
  };
  return (
    <form className="ag-stack" onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <div className="ag-field">
        <label className="ag-field__label" htmlFor="composer-text">Message</label>
        <textarea id="composer-text" className="ag-field__input" rows={3} value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey} disabled={busy} aria-describedby="composer-hint" />
        <span id="composer-hint" className="ag-field__hint">
          {busy ? "Wait for the agent to answer." : "Enter sends. Shift+Enter adds a new line."}
        </span>
      </div>
      {send.error && <p className="ag-field__error" role="alert">{message(send.error)}</p>}
      <div className="ag-row"><Button type="submit" variant="primary" disabled={busy || send.isPending || !text.trim()}>Send</Button></div>
    </form>
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

/** One timeline item. A plain function, so no component type is made during a render (UI-6). */
function itemView(item: Item, channel: string | null) {
  switch (item.type) {
    case "user":
      return (
        <li key={item.key}>
          <article className="ag-card">
            <header className="ag-card__head">
              <h3 className="ag-card__title">You</h3>
              {item.createdAt && <span className="ag-muted"><Timestamp value={item.createdAt} /></span>}
            </header>
            <div className="ag-card__body ag-card__body--pre">{item.content}</div>
          </article>
        </li>
      );
    case "text":
    case "answer":
      return (
        <li key={item.key}>
          <article className="ag-card">
            <header className="ag-card__head"><h3 className="ag-card__title">Agent</h3></header>
            <div className="ag-card__body"><Markdown>{item.type === "text" ? item.text : item.content}</Markdown></div>
          </article>
        </li>
      );
    case "tool": {
      const state = !item.done ? "running" : item.isError ? "failed" : "succeeded";
      return (
        <li key={item.key}>
          <details className="ag-card">
            <summary>
              Tool <code>{item.name || "unknown"}</code>{" "}
              <StatusBadge tone={state}>{!item.done ? "running" : item.isError ? "error" : "done"}</StatusBadge>
            </summary>
            {item.input === undefined && item.output === undefined && <p className="ag-muted">No input or output to show.</p>}
            {item.input !== undefined && <><p className="ag-muted">Input</p><JsonViewer value={item.input} /></>}
            {item.output !== undefined && <><p className="ag-muted">Output</p><JsonViewer value={item.output} /></>}
          </details>
        </li>
      );
    }
    case "error":
      return (
        <li key={item.key}>
          <article className="ag-card">
            <header className="ag-card__head"><h3 className="ag-card__title">Error</h3><StatusBadge tone="failed">error</StatusBadge></header>
            <div className="ag-card__body ag-card__body--pre">{item.text}</div>
          </article>
        </li>
      );
    case "marker":
      return <li key={item.key} className="ag-muted">{MARKER[item.kind]}</li>;
    case "run": {
      const outcome = item.finished ? String(item.finished.outcome ?? "") : undefined;
      // The server knows the trigger once the run ends, so a live viewer gets it on run.finished.
      const trigger = item.started?.prompt ?? item.finished?.prompt;
      const prompt = channel && typeof trigger === "string" ? trigger : null;
      return (
        <li key={item.key} className="ag-stack">
          <div className="ag-row">
            <span className="ag-muted">Run{item.started ? ` · ${String(item.started.source ?? item.started.type ?? "")} · job ${String(item.started.job_id)}` : ""}</span>
            <StatusBadge tone={outcome === undefined ? "running" : tone(outcome)}>{outcome ?? "running"}</StatusBadge>
          </div>
          {prompt && (
            <article className="ag-card">
              <header className="ag-card__head"><h3 className="ag-card__title">Trigger</h3></header>
              <div className="ag-card__body ag-card__body--pre">{prompt}</div>
            </article>
          )}
          <ol className="ag-list">{item.items.map((i) => itemView(i, channel))}</ol>
        </li>
      );
    }
  }
}

function Runs({ runs, events, admin }: { runs: RunRow[]; events: StreamEvent[]; admin: boolean }) {
  return (
    <details className="ag-card">
      <summary>Runs ({runs.length})</summary>
      <ul className="ag-list">
        {runs.map((r) => {
          const calls = events.filter((e) => e.kind === "tool.called" && e.execution_id === r.execution_id);
          return (
            <li key={r.execution_id} className="ag-stack">
              <div className="ag-row">
                <StatusBadge tone={tone(r.status)}>{r.status}</StatusBadge>
                <span>{r.agent_type ?? "—"} · {r.model ?? "—"} · tokens {r.input_tokens ?? 0} / {r.output_tokens ?? 0}</span>
                {admin && <Link to={`/admin/jobs?job=${r.job_id}`}>Job {r.job_id}</Link>}
              </div>
              {calls.length > 0 && (
                <div>
                  <p className="ag-muted">Toolbox calls</p>
                  <ul className="ag-list">
                    {calls.map((c) => <li key={c.id}><code>{String(c.payload.tool_name ?? "")}</code>: {String(c.payload.outcome ?? "")}</li>)}
                  </ul>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

export function View({ threadId }: { threadId: number }) {
  const qc = useQueryClient();
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
  const items = [...retained(rows, events), ...buildItems(events)];
  const detail = thread.data;
  const channel = detail && detail.channel !== "panel" ? detail.channel : null;
  const last = rows.map((m) => m.role).lastIndexOf("user");
  const state = last === -1 ? null : turnState(rows, last, detail?.runs.some((r) => r.status === "running") ?? false);
  const blocked = rows.filter((m) => m.blocked_reason === "paused_unrecoverable");

  return (
    <section className="ag-stack" aria-label="Conversation">
      <div className="ag-row"><ConnectionStatus state={stream} onReconnect={reconnect} /></div>
      {detail && detail.runs.length > 0 && <Runs runs={detail.runs} events={events} admin={admin} />}
      {store.hasOlder && (
        <div className="ag-row"><Button onClick={() => older.mutate()} disabled={older.isPending}>Load older</Button></div>
      )}
      {older.error && (
        <p className="ag-field__error" role="alert">
          {older.error instanceof ApiError && older.error.status === 409 ? "Older events were removed." : message(older.error)}
        </p>
      )}
      {/* The scroll box is focusable, so a keyboard user can scroll it. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div ref={box} onScroll={onScroll} tabIndex={0} role="region" aria-label="Timeline" style={{ maxHeight: "65vh", overflowY: "auto" }}>
        <ol className="ag-list" aria-live="polite">{items.map((i) => itemView(i, channel))}</ol>
        {items.length === 0 && (
          <EmptyState title="No messages yet">{channel ? undefined : "Write the first message below."}</EmptyState>
        )}
      </div>
      {unseen > 0 && <div className="ag-row"><Button variant="primary" onClick={jump}>{unseen} new events ↓</Button></div>}
      {state && state !== "succeeded" && (
        <JobStatusCard title="Agent turn" state={state}
          detail={rows[last].blocked_reason === "paused_unrecoverable" ? "This turn cannot resume by itself."
            : rows[last].blocked_reason === "paused" ? "This turn is paused and resumes by itself." : undefined} />
      )}
      {blocked.map((m) => (
        <div key={m.id} className="ag-row">
          <Button onClick={() => unblock.mutate(m.id)} disabled={unblock.isPending}>Unblock the conversation</Button>
        </div>
      ))}
      {thread.error && <ErrorState message={message(thread.error)} onRetry={() => void thread.refetch()} />}
      {detail && (channel
        ? <p className="ag-muted">Read-only: this conversation comes from {channel}.</p>
        : <Composer threadId={threadId} busy={inFlight(rows)} />)}
    </section>
  );
}

export default function ConversationsPage() {
  const { threadId } = useParams();
  const id = threadId && /^[0-9]{1,19}$/.test(threadId) ? Number(threadId) : null;
  return (
    <div className="ag-stack">
      <PageHeader title="Conversations" description="Talk to an agent. Each message starts one run." />
      <div className="ag-row" style={{ alignItems: "flex-start" }}>
        <div style={{ flex: "1 1 220px", maxWidth: 320 }}><Rail current={id} /></div>
        <div style={{ flex: "3 1 320px", minWidth: 0 }}>
          {id === null ? <EmptyState title="Pick a conversation">Or start a new one.</EmptyState> : <View key={id} threadId={id} />}
        </div>
      </div>
    </div>
  );
}
