// The stream's own data: in-flight assistant text and tool rows, keyed by execution_id.
// Persisted rows never live here (they come from REST). Bounded (CODE-8): per execution
// 64 KiB of text and 200 tool rows, and 8 executions; the oldest goes first at each cap.
import type { StreamEvent } from "@agento/api";

export const MAX_TEXT_BYTES = 64 * 1024;
export const MAX_TOOL_ROWS = 200;
export const MAX_EXECUTIONS = 8;

export interface ToolRow { id: number; tool_name: string; outcome: string }
export interface Execution { text: string; tools: ToolRow[] }

const encoder = new TextEncoder();

/** Keep the last `max` UTF-8 bytes of `s` (cut on a character boundary). */
function tail(s: string, max: number): string {
  if (encoder.encode(s).length <= max) return s;
  let out = s.slice(-max);
  while (encoder.encode(out).length > max) out = out.slice(1);
  return out;
}

export class TransientStore {
  private readonly runs = new Map<string, Execution>();
  private listeners = new Set<() => void>();
  private version = 0;

  subscribe = (l: () => void) => { this.listeners.add(l); return () => { this.listeners.delete(l); }; };
  getVersion = () => this.version;

  private changed() { this.version += 1; this.listeners.forEach((l) => l()); }

  private run(id: string): Execution {
    let r = this.runs.get(id);
    if (r) { this.runs.delete(id); this.runs.set(id, r); return r; }
    r = { text: "", tools: [] };
    this.runs.set(id, r);
    while (this.runs.size > MAX_EXECUTIONS) this.runs.delete(this.runs.keys().next().value as string);
    return r;
  }

  /** Returns true when the event was stream-only data and was kept. */
  apply(e: StreamEvent): boolean {
    if (!e.execution_id) return false;
    if (e.kind === "assistant.delta") {
      const text = typeof e.payload.text === "string" ? e.payload.text : "";
      const r = this.run(e.execution_id);
      r.text = tail(r.text + text, MAX_TEXT_BYTES);
    } else if (e.kind === "tool.called") {
      const r = this.run(e.execution_id);
      r.tools.push({ id: e.id, tool_name: String(e.payload.tool_name ?? ""), outcome: String(e.payload.outcome ?? "") });
      if (r.tools.length > MAX_TOOL_ROWS) r.tools.splice(0, r.tools.length - MAX_TOOL_ROWS);
    } else {
      return false;
    }
    this.changed();
    return true;
  }

  executions(): [string, Execution][] { return [...this.runs.entries()]; }

  size(): number { return this.runs.size; }

  clear(): void {
    if (!this.runs.size) return;
    this.runs.clear();
    this.changed();
  }
}
