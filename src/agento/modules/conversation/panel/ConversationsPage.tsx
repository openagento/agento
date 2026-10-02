import { useState, type KeyboardEvent } from "react";
import { useNavigate, useParams } from "react-router";
import { apiFetch, ApiError, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  Button, ConnectionStatus, EmptyState, ErrorState, JobStatusCard, LoadingState, PageHeader, SelectField, Timestamp,
} from "@agento/ui";
import { inFlight, turnState, type Message, type Thread } from "./model";
import { messagesKey, useConversation } from "./useConversation";

interface AgentView { id: number; code: string; label: string }
const message = (e: unknown) => (e instanceof ApiError ? e.message : "The request failed.");

function Rail({ current }: { current: number | null }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const threads = useQuery({ queryKey: ["threads"], queryFn: ({ signal }) => apiFetch<Thread[]>("/api/conversation/threads", { signal }) });
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
              {t.status === "archived" && <span className="ag-muted">archived</span>}
              {t.id === current && t.status !== "archived" && (
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

function View({ threadId }: { threadId: number }) {
  const qc = useQueryClient();
  const { messages, store, stream, reconnect } = useConversation(threadId);
  const unblock = useMutation({
    mutationFn: (id: number) => apiFetch(`/api/conversation/threads/${threadId}/messages/${id}/unblock`, { method: "POST" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: messagesKey(threadId) }),
  });
  if (messages.isPending) return <LoadingState />;
  if (messages.error) return <ErrorState message={message(messages.error)} onRetry={() => void messages.refetch()} />;
  const rows: Message[] = messages.data ?? [];
  const live = store.executions();
  const pending = inFlight(rows);

  return (
    <section className="ag-stack" aria-label="Conversation">
      <div className="ag-row"><ConnectionStatus state={stream} onReconnect={reconnect} /></div>
      <ol className="ag-list" aria-live="polite">
        {rows.map((m, i) => {
          const state = turnState(rows, i, live.length > 0);
          return (
            <li key={m.id} className="ag-stack">
              <article className="ag-card">
                <header className="ag-card__head">
                  <h3 className="ag-card__title">{m.role === "user" ? "You" : "Agent"}</h3>
                  {m.created_at && <span className="ag-muted"><Timestamp value={m.created_at} /></span>}
                </header>
                <div className="ag-card__body ag-card__body--pre">{m.content}</div>
              </article>
              {state && state !== "succeeded" && (
                <JobStatusCard title="Agent turn" state={state}
                  detail={m.blocked_reason === "paused_unrecoverable" ? "This turn cannot resume by itself."
                    : m.blocked_reason === "paused" ? "This turn is paused and resumes by itself." : undefined} />
              )}
              {m.blocked_reason === "paused_unrecoverable" && (
                <div className="ag-row">
                  <Button onClick={() => unblock.mutate(m.id)} disabled={unblock.isPending}>Unblock the conversation</Button>
                </div>
              )}
            </li>
          );
        })}
        {live.map(([id, run]) => (
          <li key={id} className="ag-stack">
            {run.tools.map((t) => (
              <p key={t.id} className="ag-muted">Tool <code>{t.tool_name}</code>: {t.outcome}</p>
            ))}
            {run.text && (
              <article className="ag-card" aria-busy="true">
                <header className="ag-card__head"><h3 className="ag-card__title">Agent</h3><span className="ag-badge ag-badge--running">Writing</span></header>
                <div className="ag-card__body ag-card__body--pre">{run.text}</div>
              </article>
            )}
          </li>
        ))}
      </ol>
      {rows.length === 0 && <EmptyState title="No messages yet">Write the first message below.</EmptyState>}
      <Composer threadId={threadId} busy={pending} />
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
