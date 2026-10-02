import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { endSession, hub, login, queryClient as qc, QueryClientProvider, type StreamEvent, type StreamHandlers } from "@agento/api";
import { AgentoUiProvider } from "@agento/ui";
import { Composer } from "./ConversationsPage";
import { IDLE_POLL_MS, RUN_POLL_MS, type Message } from "./model";
import { MAX_EXECUTIONS, MAX_TEXT_BYTES, MAX_TOOL_ROWS, TransientStore } from "./transient";
import { REFETCH_DEBOUNCE_MS, useConversation } from "./useConversation";

const ev = (kind: string, execution_id: string | null, payload: Record<string, unknown> = {}, id = 1): StreamEvent =>
  ({ id, kind, execution_id, payload });

describe("TransientStore", () => {
  it("keeps only stream kinds that carry an execution", () => {
    const s = new TransientStore();
    expect(s.apply(ev("message.created", "e"))).toBe(false);
    expect(s.apply(ev("assistant.delta", null, { text: "x" }))).toBe(false);
    expect(s.apply(ev("assistant.delta", "e", { text: "hi" }))).toBe(true);
    expect(s.executions()).toEqual([["e", { text: "hi", tools: [] }]]);
  });

  it("caps text, tool rows and executions", () => {
    const s = new TransientStore();
    s.apply(ev("assistant.delta", "e", { text: "é".repeat(MAX_TEXT_BYTES) }));
    expect(new TextEncoder().encode(s.executions()[0][1].text).length).toBeLessThanOrEqual(MAX_TEXT_BYTES);
    for (let i = 0; i < MAX_TOOL_ROWS + 5; i++) s.apply(ev("tool.called", "e", { tool_name: "t" }, i));
    expect(s.executions()[0][1].tools).toHaveLength(MAX_TOOL_ROWS);
    expect(s.executions()[0][1].tools[0].id).toBe(5);
    for (let i = 0; i < MAX_EXECUTIONS + 2; i++) s.apply(ev("assistant.delta", `x${i}`, { text: "." }));
    expect(s.size()).toBe(MAX_EXECUTIONS);
    expect(s.executions().map(([k]) => k)).not.toContain("e");
  });
});

describe("useConversation", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("a persisted event refetches over REST; it never writes the cache", async () => {
    vi.useFakeTimers();
    let h!: StreamHandlers;
    vi.spyOn(hub, "open").mockImplementation((_id, handlers) => { h = handlers; });
    const fetchMock = vi.fn(async () => new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const setData = vi.spyOn(qc, "setQueryData");
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => useConversation(5), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    const before = fetchMock.mock.calls.length;

    act(() => { h.onEvent(ev("assistant.delta", "e", { text: "live" })); });
    expect(result.current.store.executions()[0][1].text).toBe("live");

    act(() => { h.onEvent(ev("assistant.message", "e")); h.onEvent(ev("job.claimed", "e")); });
    await act(async () => { await vi.advanceTimersByTimeAsync(REFETCH_DEBOUNCE_MS + 10); });
    expect(fetchMock.mock.calls.length).toBe(before + 1);
    expect(setData).not.toHaveBeenCalled();
  });
});

const row = (job_state: Message["job_state"]): Message => ({
  id: 1, role: "user", content: "hi", client_message_id: null, job_id: 1, job_state, created_at: null,
  blocked: false, blocked_reason: null,
});
const wrapper = ({ children }: { children: ReactNode }) => (
  <AgentoUiProvider><QueryClientProvider client={qc}>{children}</QueryClientProvider></AgentoUiProvider>
);

describe("useConversation reconcile", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); qc.clear(); });

  it("polls every 10 s while a run is in flight, 30 s after, and a missed completion clears the live rows", async () => {
    vi.useFakeTimers();
    let h!: StreamHandlers;
    vi.spyOn(hub, "open").mockImplementation((_id, handlers) => { h = handlers; });
    let rows = [row("published")];
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(rows), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { result, unmount } = renderHook(() => useConversation(9), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    act(() => { h.onEvent(ev("assistant.delta", "e", { text: "partial" })); });
    expect(result.current.store.size()).toBe(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(RUN_POLL_MS); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // The run ended, and the event that said so was missed: only the poll sees it.
    rows = [row("terminal")];
    await act(async () => { await vi.advanceTimersByTimeAsync(RUN_POLL_MS); });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.current.store.size()).toBe(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(RUN_POLL_MS); });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS - RUN_POLL_MS); });
    expect(fetchMock).toHaveBeenCalledTimes(4);

    const close = vi.spyOn(hub, "close");
    unmount();
    expect(close).toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS * 2); });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});

describe("useConversation idle reconcile", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); qc.clear(); });

  it("after a terminal-only snapshot, a turn whose events were missed shows within 30 s", async () => {
    vi.useFakeTimers();
    vi.spyOn(hub, "open").mockImplementation(() => {});
    let rows = [row("terminal")];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(rows), { status: 200 })));
    const { result } = renderHook(() => useConversation(11), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(result.current.messages.data).toHaveLength(1);
    rows = [row("terminal"), { ...row("published"), id: 2, content: "new turn" }];
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS); });
    expect(result.current.messages.data?.map((m) => m.content)).toEqual(["hi", "new turn"]);
  });
});

describe("Composer", () => {
  afterEach(() => { endSession(); vi.unstubAllGlobals(); });

  it("Enter sends once; Shift+Enter does not send; busy disables it", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ user: { id: 1 }, csrf_token: "t", expires_at: "x" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await login("a", "b");
    fetchMock.mockClear();
    const { rerender } = render(<Composer threadId={3} busy={false} />, { wrapper });
    const box = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(String(url)).toContain("/api/conversation/threads/3/messages");
    expect(JSON.parse(String(init.body))).toMatchObject({ content: "hello" });
    rerender(<Composer threadId={3} busy />);
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
  });
});
