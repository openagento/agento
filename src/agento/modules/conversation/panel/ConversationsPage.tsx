import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import {
  apiFetch, LIVE_REFETCH_MS, ApiError, useErrorText, useMutation, useQuery, useQueryClient, useSession, useUploads, type StreamEvent,
} from "@agento/api";
import {
  AppLink, Button, ChatComposer, ChatError, ChatLayout, ChatList, ChatMessage, ChatStatus, ChatTime, ConnectionStatus, EmptyState, ErrorState,
  LoadingState, MenuButton, Rail as RailFrame, Reasoning, RegenerateAction, RowMenu, SearchToggle, SelectField, SplitView, StatusBadge, SubagentRun,
  TextField, ThreadList, ToolCall, ToolGroup, TrifectaAlert, jobTone, useDocumentTitle, useFormat, useJobStatusText, useLocale, useLookup, useT, type JobState, type ThreadLink,
  type Translate,
} from "@agento/ui";
import {
  DAY_GROUPS, dayGroup, inFlight, jobTree, turnState, type JobNode, type Message, type Thread, type ThreadDetail, type ToolRisk,
} from "./model";
import { buildItems, findTurn, isSubagent, turns, type Item, type Shown, type Tool, type Turn } from "./timeline";
import { duration, useToolSummary, type Summarize } from "./tools";
import { fetchPage, fetchThread, messagesKey, threadKey, useConversation } from "./useConversation";

interface AgentView { id: number; code: string; label: string }

const dayLabels = (t: Translate): Record<(typeof DAY_GROUPS)[number], string> => ({
  Today: t("rail.group.today"), Yesterday: t("rail.group.yesterday"), "Previous 7 days": t("rail.group.week"), Older: t("rail.group.older"),
});

/** Within this many px of the bottom, the timeline follows new events. */
const FOLLOW_PX = 80;

const threadsUrl = (scope: string) => (scope === "mine" ? "/api/conversation/threads"
  : `/api/conversation/threads?scope=channels${scope === "channels" ? "" : `&channel=${encodeURIComponent(scope.slice("channel:".length))}`}`);

/** The rail's rows. "shared" is keyset paged (`{conversations, next}`): `pages` pages are read,
 *  each from the previous `next`, so a poll refreshes every page the user loaded. */
interface Rows { rows: Thread[]; more: boolean }
async function fetchThreads(scope: string, pages: number, signal: AbortSignal): Promise<Rows> {
  if (scope !== "shared") return { rows: await apiFetch<Thread[]>(threadsUrl(scope), { signal }), more: false };
  let rows: Thread[] = [];
  let next: number | null = null;
  for (let i = 0; i < pages && (i === 0 || next !== null); i++) {
    const page: { conversations: Thread[]; next: number | null } = await apiFetch(
      `/api/conversation/threads?scope=shared${next !== null ? `&before=${next}` : ""}`, { signal });
    rows = [...rows, ...page.conversations];
    next = page.next;
  }
  return { rows, more: next !== null };
}

const useViews = () => useQuery({ queryKey: ["agent-views"], queryFn: ({ signal }) => apiFetch<AgentView[]>("/api/agent-views", { signal }) });

/** `job`: the sub-agent job open in the main panel, or null for the whole conversation. */
export function Rail({ current, job = null, close }: { current: number | null; job?: number | null; close: () => void }) {
  const t = useT("conversation");
  const jobStatusText = useJobStatusText();
  const errorText = useErrorText("conversation");
  const qc = useQueryClient();
  const navigate = useNavigate();
  const admin = useSession()?.role === "admin";
  // "mine", "shared" (channel threads shared with me), "channels" (every channel thread) or "channel:<source>".
  const [scope, setScope] = useState("mine");
  const [pages, setPages] = useState(1);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const threads = useQuery<Rows>({
    queryKey: ["threads", scope, pages], refetchInterval: LIVE_REFETCH_MS,
    // Keep the loaded pages while the next one loads, never another scope's rows.
    placeholderData: (prev, last) => (last?.queryKey[1] === scope ? prev : undefined),
    queryFn: ({ signal }) => fetchThreads(scope, pages, signal),
  });
  // The channels an admin can filter by: the ones seen in the unfiltered channel list.
  const all = useQuery({
    queryKey: ["threads", "channels"], enabled: admin,
    queryFn: ({ signal }) => apiFetch<Thread[]>(threadsUrl("channels"), { signal }),
  });
  const seen = [...new Set((all.data ?? []).map((row) => row.channel))];
  // The open conversation's sub-agent jobs, from the runs its View already polls (one cache entry).
  const open = useQuery({
    queryKey: threadKey(current ?? 0), enabled: current !== null,
    queryFn: ({ signal }) => fetchThread(current!, signal),
  });
  const tree = open.data ? jobTree(open.data.jobs) : [];
  // A job's view and harness, from its newest run: run details, so only such a reader has them.
  const runOf = new Map([...(open.data?.runs ?? [])].reverse().map((r) => [r.job_id, r]));
  // Folded rows: `c<conversation id>` and `j<job id>`; everything else is unfolded.
  const [folded, setFolded] = useState<ReadonlySet<string>>(new Set());
  const fold = (key: string) => setFolded((s) => {
    const next = new Set(s);
    if (!next.delete(key)) next.add(key);
    return next;
  });
  const views = useViews();
  const viewName = new Map((views.data ?? []).map((v) => [v.id, v.label || v.code]));
  const create = useMutation({
    mutationFn: (viewId: number) => apiFetch<{ id: number }>("/api/conversation/threads", { method: "POST", json: { agent_view_id: viewId } }),
    onSuccess: (made) => { void qc.invalidateQueries({ queryKey: ["threads"] }); close(); navigate(`/conversations/${made.id}`); },
  });
  const patch = useMutation({
    mutationFn: ({ id, ...body }: { id: number; title?: string; pinned?: boolean }) =>
      apiFetch(`/api/conversation/threads/${id}`, { method: "PATCH", json: body }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["threads"] }); },
  });
  const archive = useMutation({
    mutationFn: (id: number) => apiFetch(`/api/conversation/threads/${id}`, { method: "DELETE" }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["threads"] }); },
  });

  const jobLink = ({ job: j, children }: JobNode): ThreadLink => ({
    id: `job-${j.id}`,
    title: t("subagent.numbered", { id: String(j.id) }),
    description: [runOf.get(j.id)?.agent_view, runOf.get(j.id)?.harness, jobStatusText(j.status)].filter(Boolean).join(" · "),
    status: jobTone(j.status), active: j.id === job,
    children: children.map(jobLink), collapsed: folded.has(`j${j.id}`),
    // The first click opens the job's run; a click on the open job folds its rows.
    onSelect: j.id === job && children.length ? () => fold(`j${j.id}`)
      : () => { close(); navigate(`/conversations/${current}?job=${j.id}`); },
  });
  const titleOf = (row: Thread) => row.title || t("thread.untitled", { id: String(row.id) });
  const link = (row: Thread): ThreadLink => ({
    id: row.id,
    title: titleOf(row),
    // One line per row (U9): what used to be a second line is the row's hover title.
    description: [row.channel !== "panel" ? row.channel : null, row.agent_view_id ? viewName.get(row.agent_view_id) : null,
      row.status === "archived" ? t("rail.archived") : null].filter(Boolean).join(" · ") || undefined,
    live: row.live, active: row.id === current && job === null, pinned: row.pinned,
    // A click on the open conversation folds or unfolds its sub-agent rows; from a job, it goes back.
    onSelect: row.id === current && job === null && tree.length ? () => fold(`c${row.id}`)
      : () => { close(); navigate(`/conversations/${row.id}`); },
    ...(row.id === current && tree.length ? { children: tree.map(jobLink), collapsed: folded.has(`c${row.id}`) } : {}),
    // A channel thread: only its owner may pin, rename or archive it (the route checks). The list
    // carries no owner, but "Mine" lists only the caller's own (`c.user_id = me`); a reader by share
    // and an admin in "All channels" get no actions.
    ...(row.channel === "panel" || scope === "mine" ? {
      onPin: (pinned: boolean) => patch.mutate({ id: row.id, pinned }),
      onRename: (title: string) => patch.mutate({ id: row.id, title: title || null as unknown as string }),
      actions: row.status === "archived" ? [] : [{ id: "archive", label: t("action.archive"), icon: "archive" as const, onClick: () => archive.mutate(row.id) }],
    } : {}),
  });
  // ponytail: the search filters the loaded page only (`history/page_size` rows). Add a
  // server-side `?q=` LIKE on the title when a user keeps more threads than one page.
  const needle = query.trim().toLowerCase();
  const list = (threads.data?.rows ?? []).filter((row) => needle === "" || titleOf(row).toLowerCase().includes(needle));
  const labels = dayLabels(t);
  const groups = [
    { id: "pinned", label: t("rail.group.pinned"), threads: list.filter((row) => row.pinned).map(link) },
    ...DAY_GROUPS.map((group) => ({
      id: group, label: labels[group],
      threads: list.filter((row) => !row.pinned && dayGroup(row.last_activity_at ?? row.created_at) === group).map(link),
    })),
  ];
  return (
    <RailFrame top={<>
      <div className="ag-row">
        <MenuButton label={t("rail.new")} variant="subtle" icon="new" disabled={create.isPending}
          items={(views.data ?? []).map((v) => ({ value: String(v.id), label: v.label || v.code }))}
          onSelect={(v) => create.mutate(Number(v))} />
        <SearchToggle open={searching} onToggle={() => { setSearching(!searching); setQuery(""); }} />
      </div>
      {/* Each field is its own row of the rail's column, so the search and the scope have ONE
          width and line up; a shared `ag-row` wrapped them at the rail's narrow width. */}
      {searching && (
        <TextField label={t("rail.search")} hideLabel name="thread-search" value={query}
          onChange={(e) => setQuery(e.currentTarget.value)} placeholder={t("rail.search")} />
      )}
      <SelectField label={t("rail.scope.label")} hideLabel name="thread-scope" value={scope} onChange={(v) => { setScope(v); setPages(1); }}
        options={[{ value: "mine", label: t("rail.scope.mine") }, { value: "shared", label: t("rail.scope.shared") },
          ...(admin ? [{ value: "channels", label: t("rail.scope.channels") }, ...seen.map((c) => ({ value: `channel:${c}`, label: c }))] : [])]} />
      {create.error && <p className="ag-field__error" role="alert">{errorText(create.error, "error.generic")}</p>}
      {(patch.error || archive.error) && (
        <p className="ag-field__error" role="alert">{errorText(patch.error ?? archive.error, "error.generic")}</p>
      )}
    </>}>
      {threads.isPending ? <LoadingState /> : threads.error ? <ErrorState message={errorText(threads.error, "error.generic")} /> : (
        <>
          <ThreadList groups={groups}
            empty={needle === "" ? t("rail.empty") : t("rail.no_match")} />
          {threads.data?.more && <Button onClick={() => setPages(pages + 1)} disabled={threads.isFetching}>{t("rail.load_more")}</Button>}
        </>
      )}
    </RailFrame>
  );
}

/** One request id per SUBMISSION, kept across retries of that same submission.
 *
 *  A server that committed and lost its response must answer a retry with THAT turn, so the
 *  id survives a failed attempt. It is tied to the payload: changing the text (or picking a
 *  different message to regenerate) is a new submission and takes a new id, or the retry
 *  would replay the old payload while the composer clears the new one. `done()` after a
 *  landed request, so the next submission starts fresh. */
function useRequestId() {
  const pending = useRef<{ key: string; id: string } | null>(null);
  return {
    for: (key: string) => {
      if (pending.current?.key !== key) pending.current = { key, id: crypto.randomUUID() };
      return pending.current.id;
    },
    done: () => { pending.current = null; },
  };
}

const uploadsUrl = (threadId: number) => `/api/conversation/threads/${threadId}/uploads`;

/** The composer: typing is always allowed; only Send waits for the agent (U8). */
export function Composer({ threadId, busy, stoppable = false, onStop, stopping = false, files: staged }: {
  threadId: number; busy: boolean; stoppable?: boolean; onStop?: () => void; stopping?: boolean;
  /** The page's staging, so a file dropped anywhere on the pane lands in this composer.
   *  Without it the composer stages on its own - the `+` button still works. */
  files?: ReturnType<typeof useUploads>;
}) {
  const t = useT("conversation");
  const errorText = useErrorText("conversation");
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const request = useRequestId();
  const own = useUploads(uploadsUrl(threadId), [], "conversation");
  const files = staged ?? own;
  const send = useMutation({
    mutationFn: (content: string) => apiFetch(`/api/conversation/threads/${threadId}/messages`, {
      method: "POST",
      // The ids are part of the submission's identity: replacing a file without touching the
      // text is a DIFFERENT message, and reusing the id would replay the earlier one.
      json: { content, client_message_id: request.for(`${content}\u0000${files.ids.join(",")}`),
              upload_ids: files.ids },
    }),
    // The list too: the first message gives the thread its server title (U9).
    onSuccess: () => { request.done(); setText(""); files.reset(); void qc.invalidateQueries({ queryKey: messagesKey(threadId) }); void qc.invalidateQueries({ queryKey: ["threads"] }); },
  });
  return (
    <ChatComposer value={text} onChange={setText} onSend={() => send.mutate(text)} canSend={!busy && !send.isPending}
      placeholder={t("composer.placeholder")} focusOnMount stoppable={stoppable} onStop={onStop} stopping={stopping}
      attachments={files.files} onFiles={files.add} onRemoveAttachment={files.remove}
      hint={busy ? t("composer.answering") : t("composer.hint")}
      error={send.error ? errorText(send.error, "error.generic") : undefined} />
  );
}

/** The channel composer: an operator reply that continues the external task. Shown only with
 *  the `conversation.channel_write` grant and a reference to reply to - the route checks both. */
export function ChannelComposer({ threadId, busy, stoppable = false, onStop, stopping = false }: {
  threadId: number; busy: boolean; stoppable?: boolean; onStop?: () => void; stopping?: boolean;
}) {
  const t = useT("conversation");
  const errorText = useErrorText("conversation");
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const request = useRequestId();
  const send = useMutation({
    mutationFn: (content: string) => apiFetch(`/api/conversation/threads/${threadId}/messages`, {
      method: "POST", json: { content, client_message_id: request.for(content) },
    }),
    onSuccess: () => { request.done(); setText(""); void qc.invalidateQueries({ queryKey: messagesKey(threadId) }); void qc.invalidateQueries({ queryKey: ["threads"] }); },
  });
  // No files here: a file in a channel reply belongs in the external system the reply goes
  // to, which is a different upload entirely. Text only, and the Stop toggle.
  return (
    <ChatComposer value={text} onChange={setText} onSend={() => send.mutate(text)} canSend={!busy && !send.isPending}
      placeholder={t("channel.placeholder")} stoppable={stoppable} onStop={onStop} stopping={stopping}
      hint={busy ? t("composer.answering") : t("channel.hint")}
      error={send.error ? errorText(send.error, "error.generic") : undefined} />
  );
}

const markerText = (t: Translate, kind: "gap" | "truncated") => (kind === "gap" ? t("marker.gap") : t("marker.truncated"));

const runFailed = (t: Translate, kind: string) => (kind ? t("failure.run_failed_kind", { kind }) : t("failure.run_failed"));
/** The error's own text; a run that failed with none says so, with the kind `job.failed` named. */
const errorDetails = (t: Translate, e: { text: string; failedKind?: string }) =>
  e.text || (e.failedKind === undefined ? "" : runFailed(t, e.failedKind));

/** The messages no loaded event stands for: their events were pruned (retention keeps message
 *  rows longer), so they are older than the timeline and go before it. A message newer than the
 *  oldest event waits for its own event instead. */
function retained(rows: Message[], events: StreamEvent[]): Item[] {
  const shown = new Set(events.filter((e) => e.kind === "message.created" || e.kind === "assistant.message")
    .map((e) => e.payload.message_id));
  const oldest = events[0]?.created_at;
  return rows.filter((m) => !shown.has(m.id) && (!oldest || !m.created_at || Date.parse(m.created_at) <= Date.parse(oldest)))
    .map((m): Item => (m.role === "user"
      ? { type: "user", key: `message-${m.id}`, content: m.content, messageId: m.id,
          attachments: m.attachments, createdAt: m.created_at }
      : { type: "answer", key: `message-${m.id}`, content: m.content }));
}

const raw = (v: unknown) => (v === undefined ? undefined : typeof v === "string" ? v : JSON.stringify(v, null, 2));

/** A plain function, so no component type is made during a render (UI-6). */
function toolView(tool: Tool, ctx: Ctx) {
  return (
    <ToolCall key={tool.key} summary={ctx.summary(tool.name, tool.input, !tool.done)} running={!tool.done} isError={tool.isError}
      duration={duration(ctx.locale, tool.startedAt, tool.endedAt)} input={raw(tool.input)} output={raw(tool.output)} />
  );
}

/** What a timeline item reads besides itself: the thread's runs (harness, run details only), the
 *  texts of the panel's language and, for the admin role, each run's tool risk. */
interface Ctx {
  detail: ThreadDetail | undefined; details: boolean; t: Translate; summary: Summarize; locale: string;
  risk: (executionId: string) => ToolRisk | null | undefined;
}

/** Why an assessment is incomplete, in words; "" when it does not say. */
function gaps(r: ToolRisk, t: Translate): string {
  if (r.coverage === "error" || r.tool_coverage === "error") return t("risk.gap.error");
  return [
    r.uncovered.length ? t("risk.gap.uncovered", { names: r.uncovered.join(", ") }) : null,
    r.direct === "pending" ? t("risk.gap.pending") : null,
    r.notes.includes("modules_changed") ? t("risk.gap.modules_changed") : null,
  ].filter(Boolean).join(" ");
}

/** What makes two assessments the same warning: the verdict, the tools of each group, the gaps
 *  (as codes, so the signature does not depend on the language). */
function riskSignature(risk: ToolRisk): string {
  const groups = Object.entries(risk.categories).map(([k, v]) => [k, [...(v ?? [])].sort()] as const)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  const gap = risk.coverage === "complete" ? null
    : [risk.coverage, risk.tool_coverage, [...risk.uncovered], risk.direct, risk.notes.includes("modules_changed")];
  return JSON.stringify([risk.trifecta, groups, gap]);
}

/** The run's lethal-trifecta warning (PRD §5), frozen at its start, above its turn. An old run
 *  (`null`) and a reader without the admin role get nothing. `repeat`: the same warning as the
 *  thread's previous run, so it shows compact (still red, the groups one click away). */
function riskView(key: string, risk: ToolRisk | null | undefined, t: Translate, repeat = false) {
  if (!risk) return null;
  if (risk.trifecta !== false) {
    const possible = risk.trifecta === "possible";
    const missing = risk.coverage === "complete" ? "" : gaps(risk, t);
    return (
      <li key={`${key}-risk`}>
        <TrifectaAlert title={possible ? t("risk.title.possible") : t("risk.title.certain")} categories={risk.categories}
          compact={repeat ? t("risk.same_as_last") : undefined}
          intro={<>
            {possible ? t("risk.intro.possible") : t("risk.intro.certain")}
            {missing && <><br />{missing}</>}
          </>} />
      </li>
    );
  }
  if (risk.coverage === "complete") return null;
  return (
    <li key={`${key}-risk`}>
      <div className="ag-row">
        <StatusBadge tone="blocked">{t("risk.incomplete")}</StatusBadge>
        <span className="ag-muted">{gaps(risk, t) || t("risk.incomplete_text")}</span>
      </div>
    </li>
  );
}

/** `actions` reaches the one message that offers them: Regenerate on the newest answer. */
function shownView(item: Shown, ctx: Ctx, actions?: ReactNode) {
  const { t } = ctx;
  switch (item.type) {
    case "user":
      // Every ask carries its own clock line: the first dates the conversation, each later one
      // dates that follow-up, so a long thread says when its parts happened without a hover.
      // One list item, clock line included: the timeline measures its own items to hold the
      // scroll in place, so an ask must stay ONE item however it is decorated.
      return (
        <li key={item.key}>
          <ChatTime value={item.createdAt} />
          <ChatMessage author="user" text={item.content}
            attachments={item.attachments} time={item.createdAt} />
        </li>
      );
    case "text":
      return <li key={item.key}><ChatMessage author="assistant" text={item.text} actions={actions} /></li>;
    case "answer":
      return <li key={item.key}><ChatMessage author="assistant" text={item.content} actions={actions} /></li>;
    case "reasoning":
      return <li key={item.key}><Reasoning text={item.text} live={item.live} /></li>;
    case "tool":
      return <li key={item.key}>{toolView(item, ctx)}</li>;
    case "tools":
      return (
        <li key={item.key}>
          <ToolGroup count={item.tools.length} running={item.tools.some((tool) => !tool.done)}>{item.tools.map((tool) => toolView(tool, ctx))}</ToolGroup>
        </li>
      );
    case "subagent":
      return <li key={item.key}>{subagentView(item.turn, ctx)}</li>;
    case "marker":
      return <li key={item.key} className="ag-muted">{markerText(t, item.kind)}</li>;
    case "error": // a failure no run holds (its execution is unknown); text only with run details
      return <li key={item.key}><ChatError title={t("failure.reported")} details={errorDetails(t, item) || undefined} /></li>;
  }
}

/** `abandoned`: the run stopped and the job is not finished (a pause, a re-claim, a pool
 *  wait); the timeline cannot tell which, so the card says it is not done: blocked. */
const OUTCOME: Record<string, JobState> = { succeeded: "succeeded", failed: "failed", dead: "failed", abandoned: "blocked" };

/** A sub-agent's state from its newest run: running until `run.finished`; an attempt that
 *  will run again is queued. (A card always has a `run.started`: it is what marks a child.) */
function subagentState(turn: Turn): JobState {
  if (turn.failure?.retrying) return "published";
  if (!turn.finished) return "running";
  return OUTCOME[String(turn.finished.outcome)] ?? "blocked";
}

/** "Sub-agent #12 · reviewer · claude · resumes #9". The view and the resumed job are run
 *  details (the server leaves them out), the harness comes from the run row (run details). A
 *  child whose parent run is not loaded names its parent: "Sub-agent #12 of #10". */
function subagentLabel(turn: Turn, ctx: Ctx, orphan: boolean): string {
  const { t } = ctx;
  const p = turn.first ?? {};
  const job = turn.started?.job_id ?? turn.finished?.job_id ?? p.job_id;
  const id = job === undefined ? null : String(job);
  const parent = orphan && p.parent_job_id != null ? String(p.parent_job_id) : null;
  const harness = ctx.details ? ctx.detail?.runs.find((r) => r.execution_id === turn.executionId)?.harness : null;
  return [
    id === null ? (parent === null ? t("subagent.label") : t("subagent.of", { parent }))
      : parent === null ? t("subagent.numbered", { id }) : t("subagent.numbered_of", { id, parent }),
    typeof p.agent_view === "string" ? p.agent_view : null,
    harness || null,
    p.resume_job_id != null ? t("subagent.resumes", { id: String(p.resume_job_id) }) : null,
  ].filter(Boolean).join(" · ");
}

/** A sub-agent's turn as one collapsed card: its items, nested sub-agents and its failure. */
function subagentView(turn: Turn, ctx: Ctx, orphan = false) {
  const body = [riskView(turn.key, ctx.risk(turn.executionId), ctx.t), ...turn.items.map((i) => shownView(i, ctx)),
    ...(turn.failure && !turn.failure.retrying ? [failureView(turn, ctx)] : [])].filter(Boolean);
  return (
    <SubagentRun label={subagentLabel(turn, ctx, orphan)} state={subagentState(turn)}>
      {body.length ? <ChatList>{body}</ChatList> : undefined}
    </SubagentRun>
  );
}

/** The incoming message a channel run answers: its source and reference; the trigger text
 *  only for a reader with run details (the server sends `prompt` to that reader alone). */
function triggerView(turn: Turn, detail: ThreadDetail, ctx: Ctx) {
  const { t } = ctx;
  const p = turn.first ?? {};
  const from = [String(p.source ?? detail.channel), p.reference_id ?? detail.external_ref].filter(Boolean).join(" · ");
  const prompt = turn.first?.prompt ?? turn.finished?.prompt;
  // A follow-up (`schedule_followup` or a sub-agent's wake) says what it is; its prompt says why.
  const label = [p.type === "followup" ? t("trigger.followup") : t("trigger.from", { from }),
    p.resume_job_id != null ? t("subagent.resumes", { id: String(p.resume_job_id) }) : null].filter(Boolean).join(" · ");
  return (
    <li key={`${turn.key}-trigger`}>
      {typeof prompt === "string"
        ? <ChatMessage author="incoming" label={label} text={prompt} />
        : <p className="ag-muted">{label}</p>}
    </li>
  );
}

function failureView(turn: Turn, ctx: Ctx, onRetry?: () => void) {
  const { t } = ctx;
  const f = turn.failure!;
  const text = errorDetails(t, f);
  const said = turn.maxAttempts ? t("failure.run_failed_after", { attempt: turn.attempt ?? f.count, count: turn.maxAttempts })
    : t("failure.run_failed");
  return (
    <li key={`${turn.key}-error`}>
      <ChatError title={f.failed ? t("failure.could_not_answer") : t("failure.reported")}
        details={ctx.details ? (f.count > 1 ? `${text}\n${t("failure.error_count", { count: f.count })}` : text) : undefined} onRetry={onRetry}>
        {f.failed ? said : undefined}
      </ChatError>
    </li>
  );
}

/** The one status line of the newest turn (U3), or null when nothing is pending. An open run
 *  speaks for itself; before it, the message row's job state does. */
function statusText(rows: Message[], turn: Turn | null, ctx: Ctx): { text: string; busy: boolean } | null {
  const { t } = ctx;
  const last = rows.map((m) => m.role).lastIndexOf("user");
  const state = last === -1 ? null : turnState(rows, last, false);
  if (state === "blocked") {
    return { text: rows[last].blocked_reason === "paused_unrecoverable" ? t("status.cannot_resume") : t("status.paused"), busy: false };
  }
  if (state === "failed" && rows[last].stopped) return { text: t("status.stopped"), busy: false };
  if (state === "succeeded" || state === "failed") return null;
  if (turn?.failure?.retrying) {
    return { text: t("status.retrying", { attempt: (turn.attempt ?? 1) + 1, max: turn.maxAttempts ?? 0 }), busy: true };
  }
  if (turn?.started && !turn.finished) {
    const tail = turn.items.at(-1);
    const open = tail?.type === "tool" ? (tail.done ? undefined : tail)
      : tail?.type === "tools" ? tail.tools.findLast((tool) => !tool.done) : undefined;
    if (open) return { text: `${ctx.summary(open.name, open.input, true)}…`, busy: true };
    if (tail?.type === "text" && tail.live) return null;
    return { text: turn.attempt && turn.attempt > 1 && turn.maxAttempts
      ? t("status.thinking_attempt", { attempt: turn.attempt, max: turn.maxAttempts }) : t("status.thinking"), busy: true };
  }
  if (state === "pending") return { text: t("status.queued"), busy: true };
  if (state === "published") return { text: t("status.starting"), busy: true };
  return null;
}

/** `job`: show only that sub-agent job's run, read-only (the rail's tree opens it). */
export function View({ threadId, job = null }: { threadId: number; job?: number | null }) {
  const t = useT("conversation");
  const errorText = useErrorText("conversation");
  const locale = useLocale();
  const format = useFormat();
  const lookup = useLookup();
  const summary = useToolSummary();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const admin = useSession()?.role === "admin";
  const { messages, thread, store, version, stream, loaded, loadError, retry, reconnect, resume } = useConversation(threadId);
  const box = useRef<HTMLDivElement>(null);
  // One staging per pane: the composer and the pane's drop target fill the same list.
  const paneFiles = useUploads(uploadsUrl(threadId), [], "conversation");
  /** scrollHeight before an older page was merged: the prepend keeps the visible event in place. */
  const anchor = useRef<number | null>(null);
  const [following, setFollowing] = useState(true);
  const unblock = useMutation({
    mutationFn: (id: number) => apiFetch(`/api/conversation/threads/${threadId}/messages/${id}/unblock`, { method: "POST" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: messagesKey(threadId) }),
  });
  // Stop ends the running turn; the next message continues its session.
  const stop = useMutation({
    mutationFn: () => apiFetch(`/api/conversation/threads/${threadId}/stop`, { method: "POST" }),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: messagesKey(threadId) });
      void qc.invalidateQueries({ queryKey: threadKey(threadId) });
    },
  });
  // Retry re-sends the failed turn's text as a new message: no new route (U4).
  const resendRequest = useRequestId();
  const resend = useMutation({
    // The MESSAGE is re-asked, not its text: `regenerate` copies the original's attachments
    // into the new turn, which a fresh post of the content alone would lose (CLS-1).
    mutationFn: (messageId: number) => apiFetch(`/api/conversation/threads/${threadId}/regenerate`, {
      method: "POST", json: { message_id: messageId, client_message_id: resendRequest.for(String(messageId)) },
    }),
    onSuccess: () => { resendRequest.done(); void qc.invalidateQueries({ queryKey: messagesKey(threadId) }); void qc.invalidateQueries({ queryKey: ["threads"] }); },
  });
  /** Regenerate: the same user message asked again as a new turn. A retry replays that turn;
   *  regenerating a DIFFERENT message is a different submission and takes its own id. */
  const regenRequest = useRequestId();
  const regenerate = useMutation({
    mutationFn: (messageId: number) => apiFetch(`/api/conversation/threads/${threadId}/regenerate`, {
      method: "POST",
      json: { message_id: messageId, client_message_id: regenRequest.for(String(messageId)) },
    }),
    onSuccess: () => {
      regenRequest.done();
      void qc.invalidateQueries({ queryKey: messagesKey(threadId) });
      void qc.invalidateQueries({ queryKey: ["threads"] });
    },
  });
  const archive = useMutation({
    mutationFn: () => apiFetch(`/api/conversation/threads/${threadId}`, { method: "DELETE" }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["threads"] }); navigate("/conversations"); },
  });
  const older = useMutation({
    mutationFn: () => fetchPage(threadId, store.oldest()),
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
  }, [version, following, ready, job]);

  if (loadError) return <ErrorState message={errorText(loadError, "error.generic")} onRetry={retry} />;
  if (!loaded || messages.isPending) return <LoadingState />;
  if (messages.error) return <ErrorState message={errorText(messages.error, "error.generic")} onRetry={() => void messages.refetch()} />;

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
  const risk = (executionId: string) => (admin ? detail?.runs.find((r) => r.execution_id === executionId)?.tool_risk : null);
  const ctx: Ctx = { detail, details, t, summary, locale, risk };
  // A sub-agent whose parent run is not loaded is top-level, but it is not the thread's turn.
  const newest = top.findLast((i): i is Turn => i.type === "turn" && !isSubagent(i)) ?? null;
  const status = statusText(rows, newest, ctx);
  const blocked = rows.filter((m) => m.blocked_reason === "paused_unrecoverable");
  const lastUser = rows.map((m) => m.role).lastIndexOf("user");
  // A terminal turn with no answer and no loaded failure still gets its error and Retry.
  const unanswered = !channel && lastUser !== -1 && turnState(rows, lastUser, false) === "failed"
    && !newest?.failure?.failed && !rows[lastUser].stopped;
  // Any open top-level turn, a channel's too (the server reads it from the thread's runs).
  const stoppable = detail?.stoppable === true;

  // Regenerate hangs off the newest answer's own hover row, as the chat apps do (U9), not
  // under the whole timeline. It is offered on the same condition as before.
  const answers = top.flatMap((i) => (i.type === "turn" ? (isSubagent(i) ? [] : i.items) : [i]))
    .filter((i) => i.type === "text" || i.type === "answer");
  const newestAnswer = !channel && lastUser !== -1 && !busy && !unanswered ? answers.at(-1) : undefined;
  const regenAction = <RegenerateAction onClick={() => regenerate.mutate(rows[lastUser].id)}
    disabled={regenerate.isPending} />;

  const list: ReactNode[] = [];
  let askedId: number | null = null;
  let lastRisk: string | null = null;
  for (const item of top) {
    if (item.type !== "turn") {
      if (item.type === "user") askedId = item.messageId;
      list.push(shownView(item, ctx, item === newestAnswer ? regenAction : undefined)!);
      continue;
    }
    if (isSubagent(item)) { list.push(<li key={item.key}>{subagentView(item, ctx, true)}</li>); continue; }
    const assessed = risk(item.executionId);
    const signature = assessed ? riskSignature(assessed) : null;
    const warning = riskView(item.key, assessed, t, signature !== null && signature === lastRisk);
    if (signature !== null) lastRisk = signature; // a run with no assessment (an old run) is not compared
    if (warning) list.push(warning);
    if (channel) list.push(triggerView(item, detail!, ctx));
    for (const i of item.items) list.push(shownView(i, ctx, i === newestAnswer ? regenAction : undefined)!);
    if (item.failure && !item.failure.retrying) {
      const id = askedId;
      const retryable = !channel && item === newest && item.failure.failed && !busy && id !== null;
      list.push(failureView(item, ctx, retryable && id !== null ? () => resend.mutate(id) : undefined));
    }
  }
  const run = detail?.runs[0];
  const olderButton = store.hasOlder && (
    <div className="ag-row"><Button variant="subtle" onClick={() => older.mutate()} disabled={older.isPending}>{t("view.load_older")}</Button></div>
  );

  if (job !== null) {
    const turn = findTurn(top, job);
    const prompt = turn?.first?.prompt ?? turn?.finished?.prompt;
    const body = turn ? [
      riskView("job", risk(turn.executionId), t),
      ...(typeof prompt === "string" ? [<li key="prompt"><ChatMessage author="incoming" label={t("view.task")} text={prompt} /></li>] : []),
      ...turn.items.map((i) => shownView(i, ctx)),
      ...(turn.failure && !turn.failure.retrying ? [failureView(turn, ctx)] : []),
    ] : [];
    return (
      <ChatLayout title={turn ? subagentLabel(turn, ctx, false) : t("subagent.numbered", { id: String(job) })}
        actions={stream !== "live" && <ConnectionStatus state={stream} />}
        viewportRef={box} onScroll={onScroll}
        footer={<p className="ag-muted">{t("view.read_only_subagent")} <AppLink to={`/conversations/${threadId}`}>{t("view.back")}</AppLink></p>}>
        {olderButton}
        {turn ? <ChatList>{body}</ChatList> : (
          <EmptyState title={t("view.not_loaded.title")}>{store.hasOlder ? t("view.not_loaded.older") : t("view.not_loaded.gone")}</EmptyState>
        )}
        {turn && !turn.finished && <ChatStatus>{t("view.running")}</ChatStatus>}
      </ChatLayout>
    );
  }

  return (
    <ChatLayout
      // The whole conversation is the drop target (not just the composer): a file dropped on
      // the history is a file the user meant to attach.
      onFiles={channel ? undefined : paneFiles.add}
      title={detail?.title || t("thread.untitled", { id: String(threadId) })}
      actions={<>
        {/* The connection is a status, not an action: it stays visible, and only when it is
            not live. Every action of this header is in the one menu beside it. */}
        {stream !== "live" && <ConnectionStatus state={stream} />}
        {detail && (
          <RowMenu label={t("view.actions")}
            details={details && run ? [
              { id: "status", label: t("view.detail.status"), value: lookup("conversation", `run.status.${run.status}`) ?? run.status },
              { id: "attempt", label: t("view.detail.attempt"), value: newest?.maxAttempts
                ? t("view.detail.attempt_of", { attempt: run.attempt, max: newest.maxAttempts }) : format.number(run.attempt) },
              ...(run.harness ? [{ id: "harness", label: t("view.detail.harness"), value: run.provider ? `${run.harness} · ${run.provider}` : run.harness }] : []),
              ...(run.credential ? [{ id: "credential", label: t("view.detail.credential"), value: run.credential }] : []),
              ...(run.model ? [{ id: "model", label: t("view.detail.model"), value: run.model }] : []),
              ...(run.input_tokens != null ? [{ id: "tokens", label: t("view.detail.tokens"),
                value: `${format.number(run.input_tokens)} / ${format.number(run.output_tokens ?? 0)}` }] : []),
              ...(admin && run.job_id ? [{ id: "job", label: t("view.detail.job"), value: <AppLink to={`/admin/jobs?job=${run.job_id}`}>{String(run.job_id)}</AppLink> }] : []),
            ] : []}
            actions={[
              // Every ACTION of this header is in this menu; the badge beside it is status only.
              ...(stream === "refused" ? [{ id: "reconnect", label: t("view.reconnect"), icon: "reconnect" as const, onClick: reconnect }] : []),
              ...(!channel && detail.status !== "archived"
                ? [{ id: "archive", label: t("action.archive"), icon: "archive" as const, onClick: () => archive.mutate(), disabled: archive.isPending }] : []),
            ]} />
        )}
      </>}
      viewportRef={box} onScroll={onScroll}
      overlay={unseen > 0 && <Button variant="primary" onClick={jump}>{t("view.new_events", { count: unseen })}</Button>}
      footer={detail && (channel
        ? (detail.channel_write && detail.external_ref
          ? <ChannelComposer key={threadId} threadId={threadId} busy={busy} stoppable={stoppable}
              onStop={() => stop.mutate()} stopping={stop.isPending} />
          : (
            /* No composer to host Stop: a read-only channel thread still has to be stoppable. */
            <>
              <p className="ag-muted">{t("view.read_only_channel", { channel })}</p>
              {stoppable && (
                <Button onClick={() => stop.mutate()} disabled={stop.isPending}>{t("view.stop")}</Button>
              )}
            </>
          ))
        : <Composer key={threadId} threadId={threadId} busy={busy} stoppable={stoppable}
            files={paneFiles} onStop={() => stop.mutate()} stopping={stop.isPending} />)}
    >
      {olderButton}
      {older.error && (
        <p className="ag-field__error" role="alert">
          {older.error instanceof ApiError && older.error.status === 409 ? t("view.older_removed") : errorText(older.error, "error.generic")}
        </p>
      )}
      <ChatList>{list}</ChatList>
      {list.length === 0 && (
        <EmptyState title={t("view.empty.title")}>{channel ? undefined : t("view.empty.text")}</EmptyState>
      )}
      {unanswered && (
        <ChatError title={t("failure.could_not_answer")}
          onRetry={busy ? undefined : () => resend.mutate(rows[lastUser].id)} />
      )}
      {/* With no answer to hang it off (a first turn that only failed), Regenerate still shows. */}
      {!channel && lastUser !== -1 && !busy && !unanswered && newestAnswer === undefined && (
        <div className="ag-row">{regenAction}</div>
      )}
      {regenerate.error && <p className="ag-field__error" role="alert">{errorText(regenerate.error, "error.generic")}</p>}
      {status && (
        <ChatStatus busy={status.busy}>{status.text}</ChatStatus>
      )}
      {stop.error && <p className="ag-field__error" role="alert">{errorText(stop.error, "error.generic")}</p>}
      {blocked.map((m) => (
        <div key={m.id} className="ag-row">
          <Button onClick={() => unblock.mutate(m.id)} disabled={unblock.isPending}>{t("view.unblock")}</Button>
        </div>
      ))}
      {resend.error && <p className="ag-field__error" role="alert">{errorText(resend.error, "error.generic")}</p>}
      {thread.error && <ErrorState message={errorText(thread.error, "error.generic")} onRetry={() => void thread.refetch()} />}
    </ChatLayout>
  );
}

export default function ConversationsPage() {
  const { threadId } = useParams();
  const id = threadId && /^[0-9]{1,19}$/.test(threadId) ? Number(threadId) : null;
  const jobParam = useSearchParams()[0].get("job");
  const job = jobParam && /^[0-9]{1,19}$/.test(jobParam) ? Number(jobParam) : null;
  const t = useT("conversation");
  useDocumentTitle(t("page.title"));
  return (
    <SplitView navLabel={t("page.title")} nav={(close) => <Rail current={id} job={job} close={close} />}>
      {id === null ? <EmptyState title={t("page.pick.title")}>{t("page.pick.text")}</EmptyState> : <View key={id} threadId={id} job={job} />}
    </SplitView>
  );
}
