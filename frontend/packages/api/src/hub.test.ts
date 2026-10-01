import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backoff, BACKOFF_CEILING_MS, EventSourceHub, type StreamHandlers } from "./hub";
import { assertPanelModule, RegistryContractError } from "./contract";

class FakeES {
  static all: FakeES[] = [];
  readyState = 0;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (e: MessageEvent) => void>();
  constructor(public url: string) { FakeES.all.push(this); }
  addEventListener(k: string, fn: (e: MessageEvent) => void) { this.listeners.set(k, fn); }
  close() { this.closed = true; this.readyState = 2; }
  emit(kind: string, data: unknown) { this.listeners.get(kind)?.(new MessageEvent(kind, { data: JSON.stringify(data) })); }
}

const handlers = (): StreamHandlers & Record<string, ReturnType<typeof vi.fn>> =>
  ({ onEvent: vi.fn(), onState: vi.fn(), onResync: vi.fn() });
let hub: EventSourceHub;
beforeEach(() => { FakeES.all = []; hub = new EventSourceHub((u) => new FakeES(u) as unknown as EventSource); });
afterEach(() => { hub.close(); vi.useRealTimers(); });

describe("EventSourceHub", () => {
  it("opens one stream per thread and resyncs on open", () => {
    const h = handlers();
    hub.open(7, h);
    hub.open(8, h);
    expect(FakeES.all[0].closed).toBe(true);
    expect(FakeES.all[1].url).toBe("/api/conversation/threads/8/events/stream");
    FakeES.all[1].onopen!();
    expect(h.onState).toHaveBeenLastCalledWith("live");
    expect(h.onResync).toHaveBeenCalledTimes(1);
  });

  it("delivers events and drops a malformed frame", () => {
    const h = handlers();
    hub.open(1, h);
    FakeES.all[0].emit("assistant.delta", { id: 1, kind: "assistant.delta", execution_id: "e", payload: {} });
    FakeES.all[0].listeners.get("tool.called")!(new MessageEvent("tool.called", { data: "{" }));
    expect(h.onEvent).toHaveBeenCalledTimes(1);
  });

  it("does not retry a refused stream until asked, then waits the backoff", () => {
    vi.useFakeTimers();
    const h = handlers();
    hub.open(1, h);
    const es = FakeES.all[0];
    es.readyState = 2;
    es.onerror!();
    expect(h.onState).toHaveBeenLastCalledWith("refused");
    vi.advanceTimersByTime(60_000);
    expect(FakeES.all).toHaveLength(1);
    hub.reconnect();
    vi.advanceTimersByTime(BACKOFF_CEILING_MS);
    expect(FakeES.all).toHaveLength(2);
  });

  it("reopens with no cursor on cursor_expired", () => {
    hub.open(1, handlers());
    FakeES.all[0].listeners.get("cursor_expired")!(new MessageEvent("cursor_expired"));
    expect(FakeES.all[0].closed).toBe(true);
    expect(FakeES.all).toHaveLength(2);
  });

  it("ignores events from a closed stream", () => {
    const h = handlers();
    hub.open(1, h);
    const old = FakeES.all[0];
    hub.close();
    old.emit("assistant.delta", { id: 1 });
    expect(h.onEvent).not.toHaveBeenCalled();
  });

  it("backoff stays within 50–100 % of a 30 s ceiling", () => {
    expect(backoff(0, () => 0)).toBe(500);
    expect(backoff(0, () => 1)).toBe(1000);
    expect(backoff(20, () => 1)).toBe(BACKOFF_CEILING_MS);
  });
});

describe("assertPanelModule", () => {
  const ok = { contractVersion: 1, id: "x", routes: [{ path: "/x", load: async () => ({ default: () => null }) }], availability: { probe: "/api/x/ping" } };
  it("accepts a valid module", () => expect(assertPanelModule("x", ok)).toBe(ok));
  it.each([
    [{ ...ok, contractVersion: 2 }, /contractVersion/],
    [{ ...ok, id: "y" }, /directory/],
    [{ ...ok, routes: [] }, /no routes/],
    [{ ...ok, availability: { probe: "/api/other/ping" } }, /probe/],
  ])("names a bad module", (m, msg) => {
    expect(() => assertPanelModule("x", m)).toThrow(RegistryContractError);
    expect(() => assertPanelModule("x", m)).toThrow(msg);
  });
});
