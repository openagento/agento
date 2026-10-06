// The thread timeline (E9 §3.8): one store of events keyed by id, filled by the timeline pages,
// the replay and the stream alike, so an event seen twice renders once. `buildItems` is the
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

  subscribe = (l: () => void) => { this.listeners.add(l); return () => { this.listeners.delete(l); }; };
  getVersion = () => this.version;

  /** Idempotent: an id already held is skipped. Returns how many events were new. An older page
   *  also says whether more lie before it (`hasOlder`). */
  merge(origin: Origin, events: StreamEvent[], hasOlder?: boolean): number {
    if (hasOlder !== undefined) this.hasOlder = hasOlder;
    if (origin === "auto" && this.behind) {
      const unseen = this.unseen;
      for (const e of events) {
        if (e.id <= Math.max(this.counted, this.ids.at(-1) ?? 0)) continue;
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
      added += 1;
    }
    if (!added) return 0;
    this.ids.sort((a, b) => a - b);
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

  private trim(): void {
    if (this.ids.length <= MAX_EVENTS) return;
    for (const id of this.ids.splice(0, this.ids.length - MAX_EVENTS)) this.byId.delete(id);
    this.hasOlder = true;
  }

  private changed(): void {
    this.version += 1;
    this.listeners.forEach((l) => l());
  }

  events(): StreamEvent[] { return this.ids.map((id) => this.byId.get(id)!); }
  oldestId(): number | undefined { return this.ids[0]; }
  newestId(): number | undefined { return this.ids.at(-1); }
}

export interface Run {
  type: "run"; key: string; executionId: string;
  started: Record<string, unknown> | null; finished: Record<string, unknown> | null; items: Item[];
}
export interface Tool {
  type: "tool"; key: string; name: string; input?: unknown; output?: unknown; done: boolean; isError: boolean;
}
export type Item =
  | { type: "user"; key: string; content: string; createdAt: string | null }
  | { type: "text"; key: string; text: string }
  | { type: "answer"; key: string; content: string }
  | { type: "error"; key: string; text: string }
  | { type: "marker"; key: string; kind: "gap" | "truncated" }
  | Tool
  | Run;

const str = (v: unknown) => (typeof v === "string" ? v : "");

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
  for (const e of events) {
    const key = String(e.id);
    let into = top;
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
      case "assistant.text":
      case "assistant.delta":
        if (!hidden.has(e.id)) into.push({ type: "text", key, text: str(e.payload.text) });
        break;
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
        into.push({ type: "error", key, text: `The run failed${e.payload.kind ? ` (${str(e.payload.kind)})` : ""}.` });
        break;
      case "gap":
      case "truncated":
        into.push({ type: "marker", key, kind: e.kind });
        break;
      // tool.called is listed per run in the runs panel; job.* states come from the messages.
    }
  }
  return top;
}
