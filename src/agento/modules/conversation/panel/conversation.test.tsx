import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { hub, queryClient as qc, type StreamEvent, type StreamHandlers } from "@agento/api";
import { Providers as wrapper, stubApi, teardown } from "../../../../../frontend/panel/src/routes/admin/testing";
import { Composer, Rail, View } from "./ConversationsPage";
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

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
/** Answers like `answer` after the first call, which fails with a 500 — a write that may have committed. */
const failFirst = (answer: unknown) => {
  let failed = false;
  return () => (failed ? answer : (failed = true, json({ error: "boom" }, 500)));
};

function captureHub() {
  const opened: { handlers: StreamHandlers; after?: number }[] = [];
  vi.spyOn(hub, "open").mockImplementation((_id, handlers, after) => { opened.push({ handlers, after }); });
  return opened;
}

afterEach(() => { teardown(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("useConversation", () => {
  it("opens the stream after the newest page's id; a frame merges once; a persisted kind refetches over REST", async () => {
    vi.useFakeTimers();
    const opened = captureHub();
    const api = await stubApi({
      "/api/conversation/threads/5/timeline": () => page([ev("message.created", null, { content: "hi" }, 7)]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
    const setData = vi.spyOn(qc, "setQueryData");
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => useConversation(5), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(opened).toHaveLength(1);
    expect(opened[0].after).toBe(7);
    const before = api.calls("GET", "/api/conversation/threads/5/messages").length;

    act(() => {
      opened[0].handlers.onEvent(ev("assistant.text", "e", { text: "live" }, 8));
      opened[0].handlers.onEvent(ev("assistant.text", "e", { text: "live" }, 8));
    });
    expect(result.current.store.events().map((e) => e.id)).toEqual([7, 8]);

    act(() => { opened[0].handlers.onEvent(ev("assistant.message", "e", {}, 9)); opened[0].handlers.onEvent(ev("job.claimed", "e", {}, 10)); });
    await act(async () => { await vi.advanceTimersByTimeAsync(REFETCH_DEBOUNCE_MS + 10); });
    expect(api.calls("GET", "/api/conversation/threads/5/messages").length).toBe(before + 1);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["threads"] });   // the list moves too
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
    const api = await stubApi({
      "/api/conversation/threads/9/timeline": () => page([]),
      "/api/conversation/threads/9/messages": () => rows,
      "/api/conversation/threads/9": () => thread(),
    });
    const n = () => api.calls("GET", "/api/conversation/threads/9/messages").length;
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
      finished_at: null, type: "conversation", harness: "claude", model: "m", input_tokens: 1, output_tokens: 2 }] });
    let detail = run("running");
    let replay: StreamEvent[] = [];
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page([ev("run.started", "x", { job_id: 3 }, 4)]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => detail,
      "GET /api/conversation/threads/5/events?after=4": () => replay,
      "GET /api/conversation/threads/5/events?after=6": () => [],
    });
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
    await stubApi({
      "/api/conversation/threads/5/events": (_b: unknown, u: URL) => {
        asked.push(u.search);
        if (u.search === "?after=4") return new Promise<StreamEvent[]>((r) => { release = r; });
        return u.search === "?after=6" ? [ev("assistant.text", "x", { text: "c" }, 7)] : [];
      },
      "/api/conversation/threads/5/timeline": () => page(newest),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
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
    await stubApi({
      "/api/conversation/threads/5/events": () => json({ error: "cursor_expired" }, 409),
      "/api/conversation/threads/5/timeline": () => page(newest),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
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
    const api = await stubApi({
      "/api/conversation/threads/5/timeline": () => page(newest),
      "GET /api/conversation/threads/5/events?after=4": () => [],
      "GET /api/conversation/threads/5/events?after=90": () => [],
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
    const { result } = renderHook(() => useConversation(5), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    const replays = api.calls("GET", "/api/conversation/threads/5/events").length;
    act(() => { result.current.store.follow(false); });
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS); });
    expect(api.calls("GET", "/api/conversation/threads/5/events").length).toBe(replays);

    newest = [ev("run.finished", "x", {}, 90)];
    await act(async () => { result.current.resume(); await vi.advanceTimersByTimeAsync(10); });
    expect(result.current.store.events().map((e) => e.id)).toEqual([90]);
    await act(async () => { await vi.advanceTimersByTimeAsync(IDLE_POLL_MS); });
    expect(api.fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.includes("/events")).at(-1)).toContain("after=90");
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
    await stubApi({
      "/api/conversation/threads/11/timeline": () => page([]),
      "/api/conversation/threads/11/messages": () => rows,
      "/api/conversation/threads/11": () => thread(),
    });
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
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page(newest),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
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
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page([user(10), user(11)], true),
      "GET /api/conversation/threads/5/timeline?before=10": () => page([user(8), user(9)], false),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
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
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page([
        ev("run.started", "x", { job_id: 3, source: "jira", prompt: "PROJ-1 is due" }, 1),
        ev("assistant.message", "x", { content: "Done." }, 2),
      ]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread("jira"),
    });
    render(<View threadId={5} />, { wrapper });
    expect(await screen.findByText("Read-only: this conversation comes from jira.")).toBeInTheDocument();
    expect(screen.getByText("PROJ-1 is due")).toBeInTheDocument();
    expect(await screen.findByText("Done.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  });

  it("with run details the info icon names harness, provider, credential and model; Archive is in the row menu", async () => {
    captureHub();
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page([]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => ({ ...thread(), run_details: true, runs: [{ execution_id: "x", attempt: 1,
        status: "failed", started_at: null, finished_at: null, harness: "codex", provider: "openai",
        credential: "team-1", model: "gpt-x" }] }),
    });
    render(<View threadId={5} />, { wrapper });
    // The header keeps one overflow menu (U9), not a row of icons.
    fireEvent.click(await screen.findByRole("button", { name: "Conversation actions" }));
    expect(await screen.findByRole("menuitem", { name: "Archive" })).toBeInTheDocument();
    expect(await screen.findByText("codex · openai")).toBeInTheDocument();
    expect(screen.getByText("team-1")).toBeInTheDocument();
    expect(screen.getByText("gpt-x")).toBeInTheDocument();
  });

  it("a refused stream offers Reconnect in the SAME menu, and the badge stays visible", async () => {
    const opened = captureHub();
    const reconnect = vi.spyOn(hub, "reconnect").mockImplementation(() => {});
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page([]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
    render(<View threadId={5} />, { wrapper });
    await vi.waitFor(() => expect(opened).toHaveLength(1));
    act(() => { opened[0].handlers.onState("refused"); });

    expect(await screen.findByRole("status")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Conversation actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Reconnect" }));
    expect(reconnect).toHaveBeenCalled();
  });

});

describe("the cap while reading history", () => {
  it("scrolled up, live events are counted and the history stays; following again reloads the newest page", async () => {
    const opened = captureHub();
    let newest = Array.from({ length: MAX_EVENTS }, (_, i) => user(1001 + i));
    const api = await stubApi({
      "/api/conversation/threads/5/timeline": () => page(newest, true),
      "GET /api/conversation/threads/5/timeline?before=1001": () => page([user(999), user(1000)], false),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
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
    const reloads = api.calls("GET", "/api/conversation/threads/5/timeline").length;
    fireEvent.click(screen.getByRole("button", { name: `${2 * MAX_EVENTS} new events ↓` }));
    await screen.findByText(`m${live.at(-1)!.id}`);
    expect(api.calls("GET", "/api/conversation/threads/5/timeline").length).toBe(reloads + 1);
    expect(screen.queryByText("m999")).toBeNull();
    const shown = [...box.querySelectorAll("li")].map((li) => Number(li.textContent!.match(/m(\d+)/)![1]));
    expect(shown).toEqual(newest.map((e) => e.id));
  }, 60_000);
});

describe("a reload held while the operator scrolls up again", () => {
  /** The newest page resolves at once until `hold` is set; then it waits for `release`. */
  async function held(events409: () => boolean) {
    let newest = [user(10), user(11)];
    let hold = false;
    let release: (() => void) | null = null;
    await stubApi({
      "/api/conversation/threads/5/events": () => (events409() ? json({ error: "cursor_expired" }, 409) : []),
      "GET /api/conversation/threads/5/timeline?before=10": () => page([user(8), user(9)]),
      "/api/conversation/threads/5/timeline": async () => {
        const body = page(newest, true);
        if (hold) { hold = false; await new Promise<void>((r) => { release = r; }); }
        return body;
      },
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
    });
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

  async function expectUnchanged(box: HTMLElement, opened: { handlers: StreamHandlers }[], net: Awaited<ReturnType<typeof held>>) {
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
    const net = await held(() => false);
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
    const net = await held(() => expired);
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
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page([ev("message.created", null, { message_id: 3, role: "user", content: "kept" }, 50)]),
      "/api/conversation/threads/5/messages": () => [msg(1, "user", "old question"), msg(2, "assistant", "old answer"), msg(3, "user", "kept")],
      "/api/conversation/threads/5": () => thread(),
    });
    render(<View threadId={5} />, { wrapper });
    expect(await screen.findByText("old question")).toBeInTheDocument();
    expect(await screen.findByText("old answer")).toBeInTheDocument();
    expect(screen.getAllByText("kept")).toHaveLength(1);
    expect(screen.queryByText("No messages yet")).toBeNull();
  });

  it("an empty timeline still shows the messages", async () => {
    captureHub();
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page([]),
      "/api/conversation/threads/5/messages": () => [msg(1, "user", "old question")],
      "/api/conversation/threads/5": () => thread(),
    });
    render(<View threadId={5} />, { wrapper });
    expect(await screen.findByText("old question")).toBeInTheDocument();
    expect(screen.queryByText("No messages yet")).toBeNull();
  });

  it("a channel run shows the trigger from run.finished when run.started had none, in place", async () => {
    const opened = captureHub();
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page([ev("run.started", "x", { job_id: 3, source: "jira" }, 1)]),
      "/api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread("jira"),
    });
    render(<View threadId={5} />, { wrapper });
    const sep = (await screen.findByText("From jira")).closest("li")!;
    act(() => { opened[0].handlers.onEvent(ev("run.finished", "x", { job_id: 3, outcome: "succeeded", prompt: "PROJ-2 is due" }, 2)); });
    expect(screen.getByText("PROJ-2 is due")).toBeInTheDocument();
    expect(screen.getByText("PROJ-2 is due").closest("li")).toBe(sep);
  });
});

describe("the chat turn", () => {
  const msg = (id: number, content: string, job_state: Message["job_state"]): Message => ({
    id, role: "user", content, client_message_id: null, job_id: 7, job_state, created_at: null,
    blocked: false, blocked_reason: null,
  });
  const routes = (events: StreamEvent[], rows: () => Message[]) => ({
    "/api/conversation/threads/5/timeline": () => page(events),
    "/api/conversation/threads/5/messages": rows,
    "/api/conversation/threads/5": () => thread(),
  });

  it("a queued turn says Queued; a running tool is one summary line and the status names it", async () => {
    const opened = captureHub();
    let rows = [msg(1, "list files", "pending")];
    await stubApi(routes([ev("message.created", null, { message_id: 1, content: "list files" }, 1)], () => rows));
    render(<View threadId={5} />, { wrapper });
    expect(await screen.findByText("Queued…")).toBeInTheDocument();

    rows = [msg(1, "list files", "published")];
    act(() => {
      opened[0].handlers.onEvent(ev("run.started", "x", { job_id: 7, attempt: 1, max_attempts: 3 }, 2));
      opened[0].handlers.onEvent(ev("tool.started", "x", { tool_name: "Bash", data: { call_id: "c1", input: JSON.stringify({ command: "ls -1" }) } }, 3));
    });
    await screen.findByText("Running ls -1…");
    expect(screen.getByRole("button", { name: /Running ls -1/ })).toBeInTheDocument();
    act(() => { opened[0].handlers.onEvent(ev("tool.completed", "x", { tool_name: "Bash", data: { call_id: "c1", output: "a\nb" } }, 4)); });
    expect(screen.getByRole("button", { name: /Ran ls -1/ })).toBeInTheDocument();

    act(() => { opened[0].handlers.onEvent(ev("assistant.partial", "x", { text: "Two fi" }, 5)); });
    expect(screen.getByText("Two fi")).toBeInTheDocument();
  });

  it("a turn that failed on its last attempt shows one error, and Retry sends the question again", async () => {
    captureHub();
    const api = await stubApi({
      ...routes([
        ev("message.created", null, { message_id: 1, content: "why?" }, 1),
        ev("run.started", "x", { job_id: 7, attempt: 3, max_attempts: 3 }, 2),
        ev("error", "x", { text: "rate limited" }, 3),
        ev("job.failed", "x", { job_id: 7, attempt: 3, kind: "harness" }, 4),
      ], () => [msg(1, "why?", "terminal")]),
      "POST /api/conversation/threads/5/messages": {},
    });
    render(<View threadId={5} />, { wrapper });
    expect(await screen.findByText("The agent could not answer.")).toBeInTheDocument();
    expect(screen.getByText("The run failed after 3 of 3 attempts.")).toBeInTheDocument();
    expect(screen.getAllByText("The agent could not answer.")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/conversation/threads/5/messages")).toHaveLength(1));
    expect(api.calls("POST", "/api/conversation/threads/5/messages")[0].body).toMatchObject({ content: "why?" });
  });
});

describe("Composer", () => {
  it("Enter sends once; Shift+Enter does not send; busy keeps typing and disables Send", async () => {
    const { fetchMock } = await stubApi({ "POST /api/conversation/threads/3/messages": { message_id: 20, job_id: 21 } });
    fetchMock.mockClear();
    const invalidate = vi.spyOn(qc, "invalidateQueries");
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
    // The list refetches: the first message titles the thread on the server (review impl-1 F5).
    await vi.waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["threads"] }));
    rerender(<Composer threadId={3} busy />);
    const busyBox = screen.getByRole("textbox", { name: "Message" });
    expect(busyBox).toBeEnabled();
    fireEvent.change(busyBox, { target: { value: "next" } });
    expect(busyBox).toHaveValue("next");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    fireEvent.keyDown(busyBox, { key: "Enter" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("Regenerate and the channel composer", () => {
  const answered = [
    { id: 11, role: "user", content: "pytanie", client_message_id: "c1", job_id: 7,
      job_state: "terminal", created_at: null, blocked: false, blocked_reason: null },
    { id: 12, role: "assistant", content: "odpowiedź", client_message_id: null, job_id: null,
      job_state: null, created_at: null, blocked: false, blocked_reason: null },
  ];

  const created = { message_id: 20, job_id: 21 };

  it("Regenerate names the message, and a retry keeps the same request id", async () => {
    captureHub();
    const api = await stubApi({
      "/api/conversation/threads/5/timeline": () => page([]),
      "/api/conversation/threads/5/messages": () => answered,
      "/api/conversation/threads/5": () => thread(),
      "POST /api/conversation/threads/5/regenerate": failFirst(created),
    });
    render(<View threadId={5} />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: "Regenerate" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/conversation/threads/5/regenerate")).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/conversation/threads/5/regenerate")).toHaveLength(2));

    const sent = api.calls("POST", "/api/conversation/threads/5/regenerate")
      .map((c) => c.body);
    expect(sent[0]).toMatchObject({ message_id: 11 });
    // The failed attempt and its retry are ONE regeneration, not two.
    expect(sent[1].client_message_id).toBe(sent[0].client_message_id);
  });

  it("a send that failed after committing retries with the same request id", async () => {
    captureHub();
    const api = await stubApi({
      "/api/conversation/threads/5/timeline": () => page([]),
      "GET /api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
      "POST /api/conversation/threads/5/messages": failFirst(created),
    });
    render(<View threadId={5} />, { wrapper });
    const box = await screen.findByRole("textbox", { name: "Message" });

    fireEvent.change(box, { target: { value: "pytanie" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await vi.waitFor(() => expect(api.calls("POST", "/api/conversation/threads/5/messages")).toHaveLength(1));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    await vi.waitFor(() => expect(api.calls("POST", "/api/conversation/threads/5/messages")).toHaveLength(2));

    const sent = api.calls("POST", "/api/conversation/threads/5/messages")
      .map((c) => c.body);
    // The server may have committed the first one: the retry must replay it, not duplicate it.
    expect(sent[1].client_message_id).toBe(sent[0].client_message_id);
  });

  it("an edited retry is a new submission, with a new request id", async () => {
    captureHub();
    const api = await stubApi({
      "/api/conversation/threads/5/timeline": () => page([]),
      "GET /api/conversation/threads/5/messages": () => [],
      "/api/conversation/threads/5": () => thread(),
      "POST /api/conversation/threads/5/messages": failFirst(created),
    });
    render(<View threadId={5} />, { wrapper });
    const box = await screen.findByRole("textbox", { name: "Message" });

    fireEvent.change(box, { target: { value: "pytanie" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await vi.waitFor(() => expect(api.calls("POST", "/api/conversation/threads/5/messages")).toHaveLength(1));
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "inne pytanie" } });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    await vi.waitFor(() => expect(api.calls("POST", "/api/conversation/threads/5/messages")).toHaveLength(2));

    const sent = api.calls("POST", "/api/conversation/threads/5/messages")
      .map((c) => c.body);
    // Different text is a different turn: replaying the first id would send the old text.
    expect(sent[1].content).toBe("inne pytanie");
    expect(sent[1].client_message_id).not.toBe(sent[0].client_message_id);
  });

  it("a channel thread shows no Regenerate", async () => {
    captureHub();
    await stubApi({
      "/api/conversation/threads/5/timeline": () => page([]),
      "/api/conversation/threads/5/messages": () => answered,
      "/api/conversation/threads/5": () => ({ ...thread("jira"), external_ref: "AI-7", channel_write: true }),
    });
    render(<View threadId={5} />, { wrapper });
    await screen.findByRole("textbox", { name: "Message" });
    expect(screen.queryByRole("button", { name: "Regenerate" })).toBeNull();
  });

  it("the channel composer appears only with the grant and a reference, and posts the reply", async () => {
    captureHub();
    const routes = {
      "/api/conversation/threads/5/timeline": () => page([]),
      "/api/conversation/threads/5/messages": () => [],
    };
    const detail = (extra: Record<string, unknown>) =>
      ({ ...thread("jira"), external_ref: "AI-7", ...extra });

    for (const missing of [{ channel_write: false }, { channel_write: true, external_ref: null }]) {
      await stubApi({ ...routes, "/api/conversation/threads/5": () => detail(missing) });
      const { unmount } = render(<View threadId={5} />, { wrapper });
      expect(await screen.findByText("Read-only: this conversation comes from jira.")).toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
      unmount();
      teardown();
    }

    const api = await stubApi({ ...routes, "/api/conversation/threads/5": () => detail({ channel_write: true }),
      "POST /api/conversation/threads/5/messages": created });
    render(<View threadId={5} />, { wrapper });
    const box = await screen.findByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "odpowiedź operatora" } });
    fireEvent.keyDown(box, { key: "Enter" });

    await vi.waitFor(() => expect(api.calls("POST", "/api/conversation/threads/5/messages")).toHaveLength(1));
    expect(api.calls("POST", "/api/conversation/threads/5/messages")[0].body)
      .toMatchObject({ content: "odpowiedź operatora" });
  });
});


describe("the rail", () => {
  const row = (id: number, title: string, extra: Record<string, unknown> = {}) => ({
    id, agent_view_id: 1, title, status: "active", created_at: "2026-10-15T10:00:00Z",
    updated_at: "2026-10-15T10:00:00Z", channel: "panel", external_ref: null,
    last_activity_at: new Date().toISOString(), live: false, pinned: false, ...extra,
  });
  const views = { "/api/agent-views": () => [{ id: 1, code: "ops", label: "Ops" }] };
  /** A plain user (no admin leaf) with these threads; a rename or pin answers with the row. */
  const rail = (rows: unknown[]) => stubApi({ ...views, "/api/conversation/threads": () => rows,
    "PATCH /api/conversation/threads/1": () => rows[0] }, []);

  it("groups a pinned thread above the day groups", async () => {
    await rail([row(1, "Plain"), row(2, "Kept", { pinned: true })]);
    render(<Rail current={null} close={() => {}} />, { wrapper });
    expect(await screen.findByText("Pinned")).toBeInTheDocument();
    const labels = screen.getAllByText(/^(Pinned|Today|Kept|Plain)$/).map((e) => e.textContent);
    expect(labels.indexOf("Kept")).toBeLessThan(labels.indexOf("Plain"));
  });

  it("the search box filters the loaded list by title", async () => {
    await rail([row(1, "Invoice mail"), row(2, "Deploy notes")]);
    render(<Rail current={null} close={() => {}} />, { wrapper });
    expect(await screen.findByText("Deploy notes")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Search conversations" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Search conversations" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search conversations" }), { target: { value: "invo" } });
    expect(screen.getByText("Invoice mail")).toBeInTheDocument();
    expect(screen.queryByText("Deploy notes")).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "Search conversations" }), { target: { value: "zzz" } });
    expect(await screen.findByText("No conversation matches that.")).toBeInTheDocument();
  });

  it("the row menu pins the conversation", async () => {
    const api = await rail([row(1, "Invoice mail")]);
    render(<Rail current={null} close={() => {}} />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Conversation actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Pin" }));
    const patch = () => api.calls("PATCH", "/api/conversation/threads/1");
    await vi.waitFor(() => expect(patch()).toHaveLength(1));
    expect(patch()[0].body).toEqual({ pinned: true });
  });

  it("renaming a row PATCHes the title", async () => {
    const api = await rail([row(1, "Invoice mail")]);
    render(<Rail current={null} close={() => {}} />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Conversation actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
    const field = await screen.findByRole("textbox", { name: "Conversation title" });
    fireEvent.change(field, { target: { value: "PZU mail" } });
    fireEvent.keyDown(field, { key: "Enter" });
    const patch = () => api.calls("PATCH", "/api/conversation/threads/1");
    await vi.waitFor(() => expect(patch()).toHaveLength(1));
    expect(patch()[0].body).toEqual({ title: "PZU mail" });
  });

  it("a channel thread offers neither pin nor rename (it has no owner)", async () => {
    await stubApi({
      ...views,
      "GET /api/conversation/threads?scope=channels": () => [row(3, "PROJ-1", { channel: "jira" })],
      "/api/conversation/threads": () => [row(3, "PROJ-1", { channel: "jira" })],
    });
    render(<Rail current={null} close={() => {}} />, { wrapper });
    expect(await screen.findByText("PROJ-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Conversation actions" })).toBeNull();
  });
});
