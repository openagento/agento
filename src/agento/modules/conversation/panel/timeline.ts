// The thread timeline (E9 §3.8): one store of events keyed by id, filled by the timeline pages,
// the replay and the stream alike, so an event seen twice renders once. Events are held in time
// order, `(created_at, id)`: an imported run has high ids and old times. `buildItems` is the
// pure view of it.
import type { StreamEvent } from "@agento/api";
import type { TimelinePage } from "./model";

/** CODE-8: the store never holds more than the cap plus the older pages the operator asked for.
 *  Following live output, every `auto` merge (stream, replay, reconnect, newest page) trims to the
 *  cap. Scrolled up, `auto` events are only counted ("N new events"); following again resets the
 *  store to the newest page. An `older` merge ("Load older") never trims, so its cursor moves. */
export const MAX_EVENTS = 5000;

/** `older`: a page the operator asked for. `auto`: everything else. */
export type Origin = "older" | "auto";

export class TimelineStore {
  private readonly byId = new Map<number, StreamEvent>();
  private ids: number[] = [];
  private listeners = new Set<() => void>();
  private version = 0;
  hasOlder = false;
  /** Scrolled up: `auto` events are counted, not held. */
  behind = false;
  /** `auto` events dropped while behind: the "N new events" count. */
  unseen = 0;
  /** The highest id counted in `unseen`, so an event seen twice counts once. */
  private counted = 0;
  /** The highest id held: the stream's order, which is not the time order. */
  private maxId = 0;

  constructor(private readonly max = MAX_EVENTS) {}

  subscribe = (l: () => void) => { this.listeners.add(l); return () => { this.listeners.delete(l); }; };
  getVersion = () => this.version;

  /** Idempotent: an id already held is skipped. Returns how many events were new. An older page
   *  also says whether more lie before it (`hasOlder`). */
  merge(origin: Origin, events: StreamEvent[], hasOlder?: boolean): number {
    if (hasOlder !== undefined) this.hasOlder = hasOlder;
    if (origin === "auto" && this.behind) {
      const unseen = this.unseen;
      for (const e of events) {
        if (e.id <= Math.max(this.counted, this.maxId)) continue;
        this.counted = e.id;
        this.unseen += 1;
      }
      if (this.unseen > unseen) this.changed();
      return 0;
    }
    let added = 0;
    for (const e of events) {
      if (this.byId.has(e.id)) continue;
      this.byId.set(e.id, e);
      this.ids.push(e.id);
      this.maxId = Math.max(this.maxId, e.id);
      added += 1;
    }
    if (!added) return 0;
    this.ids.sort(this.byTime);
    if (origin === "auto") this.trim();
    this.changed();
    return added;
  }

  /** Holds the newest page and the held events newer than it (streamed after the page was read),
   *  then follows again. */
  reset(page: TimelinePage): void {
    const newer = this.ids.filter((id) => id > (page.newest_id ?? 0)).map((id) => this.byId.get(id)!);
    this.byId.clear();
    this.ids = [];
    this.maxId = 0;
    this.behind = false;
    this.unseen = 0;
    this.counted = 0;
    this.hasOlder = page.has_older;
    this.merge("auto", [...page.events, ...newer]);
    this.changed();
  }

  /** Counts the times the page left live output: a reload started before then is stale. */
  generation = 0;

  /** The page follows live output (`true`) or is scrolled up. */
  follow(on: boolean): void {
    if (!on && !this.behind) this.generation += 1;
    this.behind = !on;
  }

  /** Six fraction digits on every time (the API's `iso`), so a string compare is a time compare. */
  private byTime = (a: number, b: number): number => {
    const x = this.byId.get(a)!.created_at ?? "", y = this.byId.get(b)!.created_at ?? "";
    return x < y ? -1 : x > y ? 1 : a - b;
  };

  /** Drops the oldest events by time. */
  private trim(): void {
    if (this.ids.length <= this.max) return;
    for (const id of this.ids.splice(0, this.ids.length - this.max)) this.byId.delete(id);
    this.hasOlder = true;
  }

  private changed(): void {
    this.version += 1;
    this.listeners.forEach((l) => l());
  }

  events(): StreamEvent[] { return this.ids.map((id) => this.byId.get(id)!); }
  /** The oldest event held: the cursor of "Load older". */
  oldest(): StreamEvent | undefined { return this.byId.get(this.ids[0]); }
  /** The highest id held, the stream's cursor order. */
  newestId(): number | undefined { return this.ids.length ? this.maxId : undefined; }
}

export interface Run {
  type: "run"; key: string; executionId: string;
  started: Record<string, unknown> | null; finished: Record<string, unknown> | null; items: Item[];
}
export interface Tool {
  type: "tool"; key: string; name: string; input?: unknown; output?: unknown; done: boolean; isError: boolean;
  startedAt?: string; endedAt?: string;
}
/** `live`: a partial segment still open (B1), so more text may come. */
export interface Text { type: "text"; key: string; text: string; live?: boolean }
export interface Thought { type: "reasoning"; key: string; text: string; live: boolean }
export type Item =
  | { type: "user"; key: string; content: string; createdAt: string | null }
  | Text
  | Thought
  | { type: "answer"; key: string; content: string }
  | { type: "error"; key: string; text: string; attempt?: number }
  | { type: "marker"; key: string; kind: "gap" | "truncated" }
  | Tool
  | Run;

const str = (v: unknown) => (typeof v === "string" ? v : "");
const num = (v: unknown) => (typeof v === "number" ? v : null);

/** The open partial segment of each kind, per run (B1: stream order, no id). */
interface Open { text?: Text; reasoning?: Thought }

/** The items of a timeline, oldest first. The events of one execution sit together under
 *  its run, at the place of its first event, so two interleaved runs stay two runs. */
export function buildItems(events: StreamEvent[]): Item[] {
  // The final answer is always shown; the run's NEWEST loaded `assistant.text` is hidden when
  // it says the same. Pages load newest first, so an older page never changes which is newest.
  const newestText = new Map<string, StreamEvent>();
  const answers = new Map<string, Set<string>>();
  for (const e of events) {
    if (!e.execution_id) continue;
    if (e.kind === "assistant.text") newestText.set(e.execution_id, e);
    if (e.kind === "assistant.message") {
      const set = answers.get(e.execution_id) ?? new Set<string>();
      set.add(str(e.payload.content).trim());
      answers.set(e.execution_id, set);
    }
  }
  const hidden = new Set([...newestText].filter(([x, e]) => answers.get(x)?.has(str(e.payload.text).trim())).map(([, e]) => e.id));

  const top: Item[] = [];
  const runs = new Map<string, Run>();
  const tools = new Map<string, Tool>();
  const open = new Map<string, Open>();
  for (const e of events) {
    const key = String(e.id);
    let into = top;
    const segs = open.get(e.execution_id ?? "") ?? {};
    open.set(e.execution_id ?? "", segs);
    if (e.execution_id) {
      let run = runs.get(e.execution_id);
      if (!run) {
        run = { type: "run", key: `run-${e.execution_id}`, executionId: e.execution_id, started: null, finished: null, items: [] };
        runs.set(e.execution_id, run);
        top.push(run);
      }
      into = run.items;
      if (e.kind === "run.started") { run.started = e.payload; continue; }
      if (e.kind === "run.finished") { run.finished = e.payload; continue; }
    }
    switch (e.kind) {
      case "message.created":
        into.push({ type: "user", key, content: str(e.payload.content), createdAt: e.created_at ?? null });
        break;
      case "assistant.partial":
        if (!segs.text) into.push(segs.text = { type: "text", key, text: "", live: true });
        segs.text.text += str(e.payload.text);
        break;
      case "reasoning.partial":
        if (!segs.reasoning) into.push(segs.reasoning = { type: "reasoning", key, text: "", live: true });
        segs.reasoning.text += str(e.payload.text);
        break;
      case "assistant.text":
      case "assistant.delta": {
        // The complete fragment closes the open segment and takes its place.
        const seg = e.kind === "assistant.text" ? segs.text : undefined;
        segs.text = undefined;
        if (hidden.has(e.id)) { if (seg) into.splice(into.indexOf(seg), 1); break; }
        if (seg) { seg.text = str(e.payload.text); seg.live = false; } else into.push({ type: "text", key, text: str(e.payload.text) });
        break;
      }
      case "assistant.reasoning": {
        const seg = segs.reasoning;
        segs.reasoning = undefined;
        if (seg) { seg.text = str(e.payload.text); seg.live = false; } else into.push({ type: "reasoning", key, text: str(e.payload.text), live: false });
        break;
      }
      case "assistant.message":
        into.push({ type: "answer", key, content: str(e.payload.content) });
        break;
      case "tool.started":
      case "tool.completed": {
        const data = (e.payload.data ?? {}) as Record<string, unknown>;
        const id = data.call_id ? `${e.execution_id}:${String(data.call_id)}` : key;
        let tool = tools.get(id);
        if (!tool) {
          tool = { type: "tool", key, name: "", done: false, isError: false };
          tools.set(id, tool);
          into.push(tool);
        }
        tool.name = str(e.payload.tool_name) || tool.name;
        if ("input" in data) tool.input = data.input;
        if (e.created_at) tool[e.kind === "tool.started" ? "startedAt" : "endedAt"] = e.created_at;
        if (e.kind === "tool.completed") {
          tool.done = true;
          tool.isError = data.is_error === true;
          if ("output" in data) tool.output = data.output;
        }
        break;
      }
      case "error":
        into.push({ type: "error", key, text: str(e.payload.text) });
        break;
      case "job.failed":
        into.push({
          type: "error", key, text: `The run failed${e.payload.kind ? ` (${str(e.payload.kind)})` : ""}.`,
          ...(typeof e.payload.attempt === "number" ? { attempt: e.payload.attempt } : {}),
        });
        break;
      case "gap":
      case "truncated":
        into.push({ type: "marker", key, kind: e.kind });
        break;
      // tool.called is a toolbox audit row; job.* states come from the messages.
    }
  }
  // A run that ended leaves no segment open: what is left is its text (B1).
  for (const run of runs.values()) {
    if (!run.finished) continue;
    for (const i of run.items) if (i.type === "text" || i.type === "reasoning") i.live = false;
  }
  return nest(top);
}

// --- sub-agents: a child job's runs sit inside its parent's run ------------------------------

const jobOf = (r: Run) => num(r.started?.job_id ?? r.finished?.job_id);

/** The `agent_run` tool (any harness prefix, e.g. `mcp__agento__agent_run`). */
const AGENT_RUN = /(^|__|\.)agent_run$/;

/** The job id an `agent_run` call returned. Its output is a run detail: a reader without
 *  `conversation.run_details` gets none, so the child goes to the end of the parent run. */
export function spawnedJob(t: Tool): number | null {
  if (!AGENT_RUN.test(t.name) || t.output === undefined) return null;
  const text = typeof t.output === "string" ? t.output : JSON.stringify(t.output);
  const m = /\\?"job_id\\?"\s*:\s*(\d+)/.exec(text);
  return m ? Number(m[1]) : null;
}

/** Each run with `depth > 0` moves into the newest loaded run of its `parent_job_id`: after
 *  the `agent_run` call that returned its job id, else at the end. A child whose parent run is
 *  not loaded stays top-level. Children of a child nest the same way. */
function nest(top: Item[]): Item[] {
  const parents = new Map<number, Run>();
  for (const i of top) {
    const job = i.type === "run" ? jobOf(i) : null;
    if (job !== null) parents.set(job, i as Run);
  }
  return top.filter((i) => {
    if (i.type !== "run" || !num(i.started?.depth)) return true;
    const parent = parents.get(num(i.started?.parent_job_id) ?? NaN);
    if (!parent || parent === i) return true;
    const job = jobOf(i);
    let at = parent.items.findIndex((x) => x.type === "tool" && job !== null && spawnedJob(x) === job);
    if (at === -1) { parent.items.push(i); return false; }
    while (parent.items[at + 1]?.type === "run") at += 1;
    parent.items.splice(at + 1, 0, i);
    return false;
  });
}

// --- the chat view of the items (U2-U4) ---------------------------------------------------

export interface ToolGroupItem { type: "tools"; key: string; tools: Tool[] }
/** A sub-agent's job inside its parent's turn: every attempt of it, as one turn. */
export interface SubagentItem { type: "subagent"; key: string; turn: Turn }
export type Shown = Exclude<Item, Run> | ToolGroupItem | SubagentItem;

/** Two or more tool calls in a row fold into one group ("Used N tools"). The runs of one
 *  sub-agent job become one `subagent` item, at its first run's place. */
export function foldTools(items: Item[]): Shown[] {
  const out: Shown[] = [];
  const children = new Map<string, Run[]>();
  const pending: [SubagentItem, Run[]][] = [];
  for (const i of items) {
    if (i.type === "run") {
      const id = jobKey(i);
      const runs = children.get(id);
      if (runs) { runs.push(i); continue; }
      const item = { type: "subagent", key: i.key } as SubagentItem;
      children.set(id, [i]);
      pending.push([item, children.get(id)!]);
      out.push(item);
      continue;
    }
    const last = out.at(-1);
    if (i.type !== "tool") { out.push(i); continue; }
    if (last?.type === "tools") last.tools.push(i);
    else if (last?.type === "tool") out[out.length - 1] = { type: "tools", key: `tools-${last.key}`, tools: [last, i] };
    else out.push(i);
  }
  for (const [item, runs] of pending) item.turn = turnOf(runs);
  return out;
}

export interface Failure {
  /** The newest error text of the turn. */
  text: string;
  /** How many error items the turn's attempts carried. */
  count: number;
  /** The run failed (job.failed or a failed outcome), not only reported an error. */
  failed: boolean;
  /** Attempts are left: the job runs again by itself. */
  retrying: boolean;
}

/** One agent turn: every attempt (run) of one job. It shows the newest attempt's items, one
 *  folded failure and "attempt n of m" (B7). */
export interface Turn {
  type: "turn"; key: string;
  /** The newest attempt's execution (its row in `ThreadDetail.runs`). */
  executionId: string;
  /** `run.started` of the first attempt (the trigger) and of the newest one. */
  first: Record<string, unknown> | null; started: Record<string, unknown> | null;
  finished: Record<string, unknown> | null;
  items: Shown[]; failure: Failure | null; attempt: number | null; maxAttempts: number | null;
}

const jobKey = (r: Run) => {
  const job = r.started?.job_id ?? r.finished?.job_id;
  return job === undefined ? `x:${r.executionId}` : `j:${String(job)}`;
};

/** A sub-agent's turn (`depth > 0`). One whose parent run is not loaded stays top-level. */
export const isSubagent = (t: Turn) => (num(t.first?.depth) ?? 0) > 0;

/** The turn of job `job`, at the top level or inside a sub-agent card at any depth. */
export function findTurn(items: readonly (Shown | Exclude<Item, Run> | Turn)[], job: number): Turn | null {
  for (const i of items) {
    const t = i.type === "turn" ? i : i.type === "subagent" ? i.turn : null;
    if (!t) continue;
    if (num(t.started?.job_id ?? t.finished?.job_id ?? t.first?.job_id) === job) return t;
    const inner = findTurn(t.items, job);
    if (inner) return inner;
  }
  return null;
}

/** The top-level items with each job's runs merged into one turn, at its first run's place. */
export function turns(top: Item[]): (Exclude<Item, Run> | Turn)[] {
  const jobs = new Map<string, Run[]>();
  const out: (Exclude<Item, Run> | Run[])[] = [];
  for (const i of top) {
    if (i.type !== "run") { out.push(i); continue; }
    const id = jobKey(i);
    const runs = jobs.get(id);
    if (runs) runs.push(i);
    else { jobs.set(id, [i]); out.push(jobs.get(id)!); }
  }
  return out.map((x) => (Array.isArray(x) ? turnOf(x) : x));
}

function turnOf(runs: Run[]): Turn {
  const newest = runs.at(-1)!;
  type Err = Extract<Item, { type: "error" }>;
  const isErr = (x: Item): x is Err => x.type === "error";
  const errors = runs.flatMap((r) => r.items.filter(isErr));
  const mine = newest.items.filter(isErr);
  const attempt = num(newest.started?.attempt) ?? mine.reduce<number | null>((a, e) => e.attempt ?? a, null);
  const maxAttempts = num(newest.started?.max_attempts) ?? num(runs[0].started?.max_attempts);
  const outcome = newest.finished ? str(newest.finished.outcome) : "";
  const failed = mine.some((e) => e.attempt !== undefined) || outcome === "failed" || outcome === "dead";
  return {
    type: "turn", key: runs[0].key, executionId: newest.executionId, first: runs[0].started, started: newest.started, finished: newest.finished,
    items: foldTools(newest.items.filter((x) => !isErr(x))),
    failure: mine.length || failed ? {
      text: mine.at(-1)?.text ?? "The run failed.",
      count: errors.length,
      failed,
      retrying: failed && outcome !== "dead" && attempt !== null && maxAttempts !== null && attempt < maxAttempts,
    } : null,
    attempt, maxAttempts,
  };
}
