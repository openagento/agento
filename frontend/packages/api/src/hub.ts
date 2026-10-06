// One live stream, for the conversation on screen (PRD E8 §9). The browser's EventSource
// reconnects by itself and sends `Last-Event-ID`; the hub adds only what it cannot do:
// a pruned cursor reopens with no cursor, a refused stream is not retried in a loop, and
// logout or `pagehide` closes everything. `onResync` tells the caller to refetch over REST.
import { onEndSession } from "./session";

export type StreamState = "connecting" | "live" | "reconnecting" | "refused";

/** Every kind the stream sends. A frame is a named SSE event, and the browser drops a name
 *  nobody listens for, so a new kind must be added here. */
const STREAM_KINDS = ["message.created", "assistant.message", "assistant.text", "assistant.delta",
  "tool.started", "tool.completed", "tool.called", "error", "gap", "truncated", "run.started", "run.finished",
  "job.queued", "job.claimed", "job.failed", "job.deferred"];

export interface StreamEvent {
  id: number;
  kind: string;
  execution_id: string | null;
  payload: Record<string, unknown>;
  created_at?: string | null;
}

export interface StreamHandlers {
  onEvent: (e: StreamEvent) => void;
  onState: (s: StreamState) => void;
  /** The stream (re)opened: anything may have happened in between, refetch. */
  onResync: () => void;
}

const CLOSED = 2;
export const BACKOFF_BASE_MS = 1_000;
export const BACKOFF_CEILING_MS = 30_000;

/** Exponential backoff with jitter: 50–100 % of min(ceiling, base·2^attempt). */
export function backoff(attempt: number, random = Math.random): number {
  const cap = Math.min(BACKOFF_CEILING_MS, BACKOFF_BASE_MS * 2 ** attempt);
  return Math.round(cap * (0.5 + random() / 2));
}

export class EventSourceHub {
  private es: EventSource | null = null;
  private threadId: string | null = null;
  /** The newest id delivered (or the caller's page): a reopen by the hub resumes there. */
  private after: number | undefined;
  private handlers: StreamHandlers | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly create: (url: string) => EventSource = (url) => new EventSource(url)) {}

  /** `after`: the newest event id the caller already holds, so the stream leaves no window. */
  open(threadId: string | number, handlers: StreamHandlers, after?: number): void {
    this.close();
    this.threadId = String(threadId);
    this.after = after;
    this.handlers = handlers;
    this.attempt = 0;
    this.connect();
  }

  /** A manual reconnect after `refused`, delayed by the backoff. */
  reconnect(): void {
    if (!this.threadId || this.es || this.timer) return;
    this.handlers?.onState("connecting");
    this.timer = setTimeout(() => { this.timer = null; this.connect(); }, backoff(this.attempt));
    this.attempt += 1;
  }

  close(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.es?.close();
    this.es = null;
    this.threadId = null;
    this.handlers = null;
  }

  private connect(): void {
    const h = this.handlers;
    if (!h || !this.threadId) return;
    const query = this.after === undefined ? "" : `?after=${this.after}`;
    const es = this.create(`/api/conversation/threads/${encodeURIComponent(this.threadId)}/events/stream${query}`);
    this.es = es;
    h.onState("connecting");
    const current = () => this.es === es;
    es.onopen = () => {
      if (!current()) return;
      this.attempt = 0;
      h.onState("live");
      h.onResync();
    };
    es.onerror = (ev: Event) => {
      if (!current()) return;
      // The browser fires `onerror` for EVERY event of type `error`, and a run's error card
      // arrives as one (delivered below). Only a plain Event is a lost connection.
      if (ev instanceof MessageEvent) return;
      if (es.readyState === CLOSED) {
        // A refused answer (401, 404, 429): the browser stopped, and so does the hub.
        es.close();
        this.es = null;
        h.onState("refused");
      } else {
        h.onState("reconnecting");
      }
    };
    const deliver = (m: MessageEvent) => {
      if (!current()) return;
      let e: StreamEvent;
      try { e = JSON.parse(m.data) as StreamEvent; } catch { return; /* a malformed frame is dropped */ }
      if (typeof e.id === "number" && e.id > (this.after ?? 0)) this.after = e.id;
      h.onEvent(e);
    };
    for (const kind of STREAM_KINDS) es.addEventListener(kind, deliver as EventListener);
    es.addEventListener("cursor_expired", () => {
      if (!current()) return;
      // The cursor was pruned: a new EventSource carries no Last-Event-ID, so it starts at
      // the newest event; `onopen` then makes the caller refetch the snapshot.
      es.close();
      this.after = undefined;
      this.connect();
    });
  }
}

export const hub = new EventSourceHub();
onEndSession(() => hub.close());
if (typeof window !== "undefined") window.addEventListener("pagehide", () => hub.close());
