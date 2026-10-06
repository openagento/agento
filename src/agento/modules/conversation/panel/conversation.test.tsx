import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { endSession, hub, login, queryClient as qc, QueryClientProvider, type StreamEvent, type StreamHandlers } from "@agento/api";
import { AgentoUiProvider } from "@agento/ui";
import { Composer, View } from "./ConversationsPage";
import { IDLE_POLL_MS, RUN_POLL_MS, type Message } from "./model";
import { MAX_EVENTS } from "./timeline";
import { REFETCH_DEBOUNCE_MS, useConversation } from "./useConversation";

const ev = (kind: string, execution_id: string | null, payload: Record<string, unknown> = {}, id = 1): StreamEvent =>
  ({ id, kind, execution_id, payload, created_at: null });
const page = (events: StreamEvent[], has_older = false) => ({ events, has_older, newest_id: events.at(-1)?.id ?? null });
const thread = (channel = "panel") => ({
  id: 5, agent_view_id: 1, title: null, status: "active", created_at: null, updated_at: null, channel,
  external_ref: null, last_activity_at: null, live: false, runs: [],
});

/** A fetch double that answers by path (+ query); an unknown path is a 404, as the API would. */
function api(routes: Record<string, () => unknown>) {
  return vi.fn(async (url: URL | string) => {
    const u = new URL(String(url), "http://panel");
    const route = routes[u.pathname + u.search];
    return route ? new Response(JSON.stringify(route()), { status: 200 }) : new Response("{}", { status: 404 });
  });
}
const calls = (f: ReturnType<typeof api>, path: string) =>
  f.mock.calls.filter(([u]) => new URL(String(u), "http://panel").pathname === path).length;

const wrapper = ({ children }: { children: ReactNode }) => (
  <AgentoUiProvider><QueryClientProvider client={qc}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider></AgentoUiProvider>
);

function captureHub() {
  const opened: { handlers: StreamHandlers; after?: number }[] = [];
  vi.spyOn(hub, "open").mockImplementation((_id, handlers, after) => { opened.push({ handlers, after }); });
  return opened;
}

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); qc.clear(); });

describe("useConversation", () => {
  it("opens the stream after the newest page's id; a frame merges once; a persisted kind refetches over REST", async () => {
    vi.useFakeTimers();
    const opened = captureHub();
    const fetchMock = api({
      "/api/conversation/threads/5/timeline": () => page([ev("message.created", null, { content: "hi" }, 7)]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
    vi.stubGlobal("fetch", fetchMock);
    const setData = vi.spyOn(qc, "setQueryData");
    const { result } = renderHook(() => useConversation(5), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(opened).toHaveLength(1);
    expect(opened[0].after).toBe(7);
    const before = calls(fetchMock, "/api/conversation/threads/5/messages");

    act(() => {
      opened[0].handlers.onEvent(ev("assistant.text", "e", { text: "live" }, 8));
      opened[0].handlers.onEvent(ev("assistant.text", "e", { text: "live" }, 8));
    });
    expect(result.current.store.events().map((e) => e.id)).toEqual([7, 8]);

    act(() => { opened[0].handlers.onEvent(ev("assistant.message", "e", {}, 9)); opened[0].handlers.onEvent(ev("job.claimed", "e", {}, 10)); });
    await act(async () => { await vi.advanceTimersByTimeAsync(REFETCH_DEBOUNCE_MS + 10); });
    expect(calls(fetchMock, "/api/conversation/threads/5/messages")).toBe(before + 1);
    expect(setData).not.toHaveBeenCalled();
  });

  it("polls every 10 s while a run is in flight and 30 s after", async () => {
    vi.useFakeTimers();
    captureHub();
    const row = (job_state: Message["job_state"]): Message => ({
      id: 1, role: "user", content: "hi", client_message_id: null, job_id: 1, job_state, created_at: null,
      blocked: false, blocked_reason: null,
    });
    let rows = [row("published")];
    const fetchMock = api({
      "/api/conversation/threads/9/timeline": () => page([]),
      "/api/conversation/threads/9/messages": () => rows,
      "/api/conversation/threads/9": () => thread(),
    });
    vi.stubGlobal("fetch", fetchMock);
    const n = () => calls(fetchMock, "/api/conversation/threads/9/messages");
    const { unmount } = renderHook(() => useConversation(9), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(n()).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(RUN_POLL_MS); });
    expect(n()).toBe(2);
    rows = [row("terminal")];
    await act(async () => { await vi.advanceTimersByTimeAsync(RUN_POLL_MS); });
    expect(n()).toBe(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(RUN_POLL_MS); });
    expect(n()).toBe(3);
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS - RUN_POLL_MS); });
    expect(n()).toBe(4);

    const close = vi.spyOn(hub, "close");
    unmount();
    expect(close).toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS * 2); });
    expect(n()).toBe(4);
  });
});

describe("useConversation REST fallback", () => {
  it("with the stream down, a poll tick pages forward from the newest event and refetches the runs", async () => {
    vi.useFakeTimers();
    captureHub();
    const run = (status: string) => ({ ...thread(), runs: [{ execution_id: "x", job_id: 3, attempt: 1, status, started_at: null,
      finished_at: null, type: "conversation", agent_type: "claude", model: "m", input_tokens: 1, output_tokens: 2 }] });
    let detail = run("running");
    let replay: StreamEvent[] = [];
    const fetchMock = api({
      "/api/conversation/threads/5/timeline": () => page([ev("run.started", "x", { job_id: 3 }, 4)]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => detail,
      "/api/conversation/threads/5/events?after=4": () => replay,
      "/api/conversation/threads/5/events?after=6": () => [],
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useConversation(5), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    replay = [ev("assistant.message", "x", { message_id: 1, content: "Done." }, 5), ev("run.finished", "x", { outcome: "succeeded" }, 6)];
    detail = run("succeeded");
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS); });
    expect(result.current.store.events().map((e) => e.id)).toEqual([4, 5, 6]);
    expect(result.current.thread.data?.runs[0].status).toBe("succeeded");
  });

  it("the replay cursor moves only by replay pages: a reconnect page merged meanwhile skips nothing", async () => {
    const opened = captureHub();
    let newest = [ev("run.started", "x", {}, 4)];
    let release!: (events: StreamEvent[]) => void;
    const asked: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: URL | string) => {
      const u = new URL(String(url), "http://panel");
      const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200 });
      if (u.pathname.endsWith("/events")) {
        asked.push(u.search);
        if (u.search === "?after=4") return json(await new Promise<StreamEvent[]>((r) => { release = r; }));
        if (u.search === "?after=6") return json([ev("assistant.text", "x", { text: "c" }, 7)]);
        return json([]);
      }
      if (u.pathname.endsWith("/timeline")) return json(page(newest));
      if (u.pathname.endsWith("/messages")) return json([]);
      return json(thread());
    }));
    const { result } = renderHook(() => useConversation(5), { wrapper });
    await vi.waitFor(() => expect(asked).toEqual(["?after=4"]));
    // A reconnect merges the newest page while that replay is in flight.
    newest = [ev("assistant.text", "x", { text: "z" }, 50)];
    await act(async () => { opened[0].handlers.onResync(); opened[0].handlers.onResync(); });
    await vi.waitFor(() => expect(result.current.store.newestId()).toBe(50));
    await act(async () => { release([ev("assistant.text", "x", { text: "a" }, 5), ev("assistant.text", "x", { text: "b" }, 6)]); });
    await vi.waitFor(() => expect(asked).toEqual(["?after=4", "?after=6", "?after=7"]));
    expect(result.current.store.events().map((e) => e.id)).toEqual([4, 5, 6, 7, 50]);
  });

  it("a 409 on the forward page reloads the newest timeline page", async () => {
    vi.useFakeTimers();
    captureHub();
    let newest = [ev("run.started", "x", {}, 4)];
    const fetchMock = vi.fn(async (url: URL | string) => {
      const u = new URL(String(url), "http://panel");
      if (u.pathname.endsWith("/events")) return new Response(JSON.stringify({ error: "cursor_expired" }), { status: 409 });
      if (u.pathname.endsWith("/timeline")) return new Response(JSON.stringify(page(newest)), { status: 200 });
      if (u.pathname.endsWith("/messages")) return new Response("[]", { status: 200 });
      return new Response(JSON.stringify(thread()), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useConversation(5), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    newest = [ev("run.finished", "x", {}, 90)];
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS); });
    expect(result.current.store.events().map((e) => e.id)).toEqual([90]);
  });
});

describe("useConversation while scrolled up", () => {
  it("stops replay paging; following again reloads the newest page and moves the cursor to it", async () => {
    vi.useFakeTimers();
    captureHub();
    let newest = [ev("run.started", "x", {}, 4)];
    const fetchMock = api({
      "/api/conversation/threads/5/timeline": () => page(newest),
      "/api/conversation/threads/5/events?after=4": () => [],
      "/api/conversation/threads/5/events?after=90": () => [],
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useConversation(5), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    const replays = calls(fetchMock, "/api/conversation/threads/5/events");
    act(() => { result.current.store.follow(false); });
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS); });
    expect(calls(fetchMock, "/api/conversation/threads/5/events")).toBe(replays);

    newest = [ev("run.finished", "x", {}, 90)];
    await act(async () => { result.current.resume(); await vi.advanceTimersByTimeAsync(10); });
    expect(result.current.store.events().map((e) => e.id)).toEqual([90]);
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS); });
    expect(fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.includes("/events")).at(-1)).toContain("after=90");
  });
});

describe("useConversation idle reconcile", () => {
  it("after a terminal-only snapshot, a turn whose events were missed shows within 30 s", async () => {
    vi.useFakeTimers();
    captureHub();
    const row = (id: number, content: string, job_state: Message["job_state"]): Message => ({
      id, role: "user", content, client_message_id: null, job_id: 1, job_state, created_at: null,
      blocked: false, blocked_reason: null,
    });
    let rows = [row(1, "hi", "terminal")];
    vi.stubGlobal("fetch", api({
      "/api/conversation/threads/11/timeline": () => page([]),
      "/api/conversation/threads/11/messages": () => rows,
      "/api/conversation/threads/11": () => thread(),
    }));
    const { result } = renderHook(() => useConversation(11), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(result.current.messages.data).toHaveLength(1);
    rows = [row(1, "hi", "terminal"), row(2, "new turn", "published")];
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS); });
    expect(result.current.messages.data?.map((m) => m.content)).toEqual(["hi", "new turn"]);
  });
});

/** jsdom has no layout: each list item is 100 px high, the box shows 100 px. */
function layout(box: HTMLElement) {
  let top = 0;
  Object.defineProperty(box, "clientHeight", { configurable: true, get: () => 100 });
  Object.defineProperty(box, "scrollHeight", { configurable: true, get: () => box.querySelectorAll("li").length * 100 });
  Object.defineProperty(box, "scrollTop", { configurable: true, get: () => top, set: (v: number) => { top = v; } });
}

const user = (id: number) => ev("message.created", null, { content: `m${id}` }, id);

describe("the thread view", () => {
  it("follows new events at the bottom; scrolled up, it counts them in a button that resumes following", async () => {
    const opened = captureHub();
    let newest = [user(1), user(2)];
    vi.stubGlobal("fetch", api({
      "/api/conversation/threads/5/timeline": () => page(newest),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    }));
    render(<View threadId={5} />, { wrapper });
    const box = await screen.findByRole("region", { name: "Timeline" });
    layout(box);

    act(() => { opened[0].handlers.onEvent(user(3)); });
    expect(box.scrollTop).toBe(300);

    box.scrollTop = 0;
    fireEvent.scroll(box);
    act(() => { opened[0].handlers.onEvent(user(4)); opened[0].handlers.onEvent(user(5)); });
    expect(box.scrollTop).toBe(0);
    expect(screen.queryByText("m4")).toBeNull();
    newest = [1, 2, 3, 4, 5].map(user);
    fireEvent.click(screen.getByRole("button", { name: "2 new events ↓" }));
    await screen.findByText("m5");
    expect(box.scrollTop).toBe(500);
    expect(screen.queryByRole("button", { name: /new events/ })).toBeNull();

    // A reconnect never scrolls.
    box.scrollTop = 0;
    fireEvent.scroll(box);
    act(() => { opened[0].handlers.onState("reconnecting"); opened[0].handlers.onState("live"); });
    expect(box.scrollTop).toBe(0);
  });

  it("prepends an older page and keeps the visible event in place", async () => {
    captureHub();
    const fetchMock = api({
      "/api/conversation/threads/5/timeline": () => page([user(10), user(11)], true),
      "/api/conversation/threads/5/timeline?before=10": () => page([user(8), user(9)], false),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<View threadId={5} />, { wrapper });
    const box = await screen.findByRole("region", { name: "Timeline" });
    layout(box);
    box.scrollTop = 0;
    fireEvent.scroll(box);

    fireEvent.click(screen.getByRole("button", { name: "Load older" }));
    await screen.findByText("m8");
    expect(box.scrollTop).toBe(200);
    expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
  });

  it("a channel thread has no composer and shows the run's trigger", async () => {
    captureHub();
    vi.stubGlobal("fetch", api({
      "/api/conversation/threads/5/timeline": () => page([
        ev("run.started", "x", { job_id: 3, source: "jira", prompt: "PROJ-1 is due" }, 1),
        ev("assistant.message", "x", { content: "Done." }, 2),
      ]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread("jira"),
    }));
    render(<View threadId={5} />, { wrapper });
    expect(await screen.findByText("Read-only: this conversation comes from jira.")).toBeInTheDocument();
    expect(screen.getByText("PROJ-1 is due")).toBeInTheDocument();
    expect(await screen.findByText("Done.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  });
});

describe("the cap while reading history", () => {
  it("scrolled up, live events are counted and the history stays; following again reloads the newest page", async () => {
    const opened = captureHub();
    let newest = Array.from({ length: MAX_EVENTS }, (_, i) => user(1001 + i));
    const fetchMock = api({
      "/api/conversation/threads/5/timeline": () => page(newest, true),
      "/api/conversation/threads/5/timeline?before=1001": () => page([user(999), user(1000)], false),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<View threadId={5} />, { wrapper });
    const box = await screen.findByRole("region", { name: "Timeline" });
    layout(box);
    box.scrollTop = 0;
    fireEvent.scroll(box);
    fireEvent.click(screen.getByRole("button", { name: "Load older" }));
    const read = await screen.findByText("m999");
    expect(box.scrollTop).toBe(200);

    // Two live runs while scrolled up: counted, not held.
    const live = Array.from({ length: 2 * MAX_EVENTS }, (_, i) => user(1001 + MAX_EVENTS + i));
    act(() => { live.forEach((e) => opened[0].handlers.onEvent(e)); });
    expect(screen.getByText("m999")).toBe(read);
    expect(box.scrollTop).toBe(200);
    expect(box.querySelectorAll("li")).toHaveLength(MAX_EVENTS + 2);

    newest = live.slice(-MAX_EVENTS);
    const reloads = calls(fetchMock, "/api/conversation/threads/5/timeline");
    fireEvent.click(screen.getByRole("button", { name: `${2 * MAX_EVENTS} new events ↓` }));
    await screen.findByText(`m${live.at(-1)!.id}`);
    expect(calls(fetchMock, "/api/conversation/threads/5/timeline")).toBe(reloads + 1);
    expect(screen.queryByText("m999")).toBeNull();
    const shown = [...box.querySelectorAll("li")].map((li) => Number(li.textContent!.match(/m(\d+)/)![1]));
    expect(shown).toEqual(newest.map((e) => e.id));
  }, 60_000);
});

describe("a reload held while the operator scrolls up again", () => {
  /** The newest page resolves at once until `hold` is set; then it waits for `release`. */
  function held(events409: () => boolean) {
    let newest = [user(10), user(11)];
    let hold = false;
    let release: (() => void) | null = null;
    vi.stubGlobal("fetch", vi.fn(async (url: URL | string) => {
      const u = new URL(String(url), "http://panel");
      const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status });
      if (u.pathname.endsWith("/events")) return events409() ? json({ error: "cursor_expired" }, 409) : json([]);
      if (u.pathname.endsWith("/timeline") && u.search === "?before=10") return json(page([user(8), user(9)]));
      if (u.pathname.endsWith("/timeline")) {
        const body = page(newest, true);
        if (hold) { hold = false; await new Promise<void>((r) => { release = r; }); }
        return json(body);
      }
      if (u.pathname.endsWith("/messages")) return json([]);
      return json(thread());
    }));
    return {
      setNewest: (e: StreamEvent[]) => { newest = e; },
      hold: () => { hold = true; },
      held: () => release !== null,
      release: async () => { await act(async () => { release!(); }); },
    };
  }

  async function scrollUpAndLoadOlder(box: HTMLElement) {
    box.scrollTop = 0;
    fireEvent.scroll(box);
    fireEvent.click(screen.getByRole("button", { name: "Load older" }));
    await screen.findByText("m8");
    expect(box.scrollTop).toBe(200);
  }

  async function expectUnchanged(box: HTMLElement, opened: { handlers: StreamHandlers }[], net: ReturnType<typeof held>) {
    await net.release();
    expect(screen.getByText("m8")).toBeInTheDocument();
    expect(box.scrollTop).toBe(200);
    // Still behind: a live event is counted, not shown.
    act(() => { opened[0].handlers.onEvent(user(13)); });
    expect(screen.queryByText("m13")).toBeNull();
    // A later resume still resets to the newest page.
    net.setNewest([10, 11, 12, 13].map(user));
    fireEvent.click(screen.getByRole("button", { name: /new events ↓/ }));
    await screen.findByText("m13");
    expect(screen.queryByText("m8")).toBeNull();
  }

  it("a delayed resume reload does nothing", async () => {
    const opened = captureHub();
    const net = held(() => false);
    render(<View threadId={5} />, { wrapper });
    const box = await screen.findByRole("region", { name: "Timeline" });
    layout(box);
    box.scrollTop = 0;
    fireEvent.scroll(box);
    act(() => { opened[0].handlers.onEvent(user(12)); });
    net.hold();
    fireEvent.click(screen.getByRole("button", { name: "1 new events ↓" }));
    await vi.waitFor(() => expect(net.held()).toBe(true));
    await scrollUpAndLoadOlder(box);
    await expectUnchanged(box, opened, net);
  });

  it("a delayed 409 reload does nothing", async () => {
    const opened = captureHub();
    let expired = false;
    const net = held(() => expired);
    render(<View threadId={5} />, { wrapper });
    const box = await screen.findByRole("region", { name: "Timeline" });
    layout(box);
    expired = true;
    net.hold();
    await act(async () => { await qc.invalidateQueries({ queryKey: ["messages", 5] }); });
    await vi.waitFor(() => expect(net.held()).toBe(true));
    await scrollUpAndLoadOlder(box);
    await expectUnchanged(box, opened, net);
  });
});

describe("the thread view, history and triggers", () => {
  const msg = (id: number, role: "user" | "assistant", content: string): Message => ({
    id, role, content, client_message_id: null, job_id: null, job_state: null, created_at: "2026-01-01T00:00:00Z",
    blocked: false, blocked_reason: null,
  });

  it("shows retained messages whose events were pruned, and never twice", async () => {
    captureHub();
    vi.stubGlobal("fetch", api({
      "/api/conversation/threads/5/timeline": () => page([ev("message.created", null, { message_id: 3, role: "user", content: "kept" }, 50)]),
      "/api/conversation/threads/5/messages": () => [msg(1, "user", "old question"), msg(2, "assistant", "old answer"), msg(3, "user", "kept")],
      "/api/conversation/threads/5": () => thread(),
    }));
    render(<View threadId={5} />, { wrapper });
    expect(await screen.findByText("old question")).toBeInTheDocument();
    expect(await screen.findByText("old answer")).toBeInTheDocument();
    expect(screen.getAllByText("kept")).toHaveLength(1);
    expect(screen.queryByText("No messages yet")).toBeNull();
  });

  it("an empty timeline still shows the messages", async () => {
    captureHub();
    vi.stubGlobal("fetch", api({
      "/api/conversation/threads/5/timeline": () => page([]),
      "/api/conversation/threads/5/messages": () => [msg(1, "user", "old question")],
      "/api/conversation/threads/5": () => thread(),
    }));
    render(<View threadId={5} />, { wrapper });
    expect(await screen.findByText("old question")).toBeInTheDocument();
    expect(screen.queryByText("No messages yet")).toBeNull();
  });

  it("a channel run shows the trigger from run.finished when run.started had none, in place", async () => {
    const opened = captureHub();
    vi.stubGlobal("fetch", api({
      "/api/conversation/threads/5/timeline": () => page([ev("run.started", "x", { job_id: 3, source: "jira" }, 1)]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread("jira"),
    }));
    render(<View threadId={5} />, { wrapper });
    const sep = (await screen.findByText(/job 3/)).closest("li")!;
    act(() => { opened[0].handlers.onEvent(ev("run.finished", "x", { job_id: 3, outcome: "succeeded", prompt: "PROJ-2 is due" }, 2)); });
    expect(screen.getByText("PROJ-2 is due")).toBeInTheDocument();
    expect(screen.getByText(/job 3/).closest("li")).toBe(sep);
  });
});

describe("Composer", () => {
  afterEach(() => { endSession(); });

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
