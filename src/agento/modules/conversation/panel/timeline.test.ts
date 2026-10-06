import { describe, expect, it } from "vitest";
import type { StreamEvent } from "@agento/api";
import { buildItems, MAX_EVENTS, TimelineStore, type Item, type Run } from "./timeline";

const ev = (id: number, kind: string, execution_id: string | null, payload: Record<string, unknown> = {}): StreamEvent =>
  ({ id, kind, execution_id, payload, created_at: null });
const text = (id: number, t: string, x = "e") => ev(id, "assistant.text", x, { seq: id, text: t });
const answer = (id: number, c: string, x = "e") => ev(id, "assistant.message", x, { message_id: id, content: c });
const run = (items: Item[], x = "e") => items.find((i): i is Run => i.type === "run" && i.executionId === x)!;
const shown = (events: StreamEvent[]) => run(buildItems(events)).items.map((i) =>
  (i.type === "text" ? `text:${i.text}` : i.type === "answer" ? `answer:${i.content}` : i.type));

describe("TimelineStore", () => {
  const ids = (s: TimelineStore) => s.events().map((e) => e.id);
  const span = (from: number, n: number) => Array.from({ length: n }, (_, i) => text(from + i, "."));

  it("renders an event once when a page, a replay and a stream frame all carry it", () => {
    const s = new TimelineStore();
    expect(s.merge("auto", [ev(1, "message.created", null, { content: "hi" }), text(2, "a")])).toBe(2);
    expect(s.merge("auto", [text(2, "a")])).toBe(0);
    expect(s.merge("auto", [text(2, "a"), text(3, "b")])).toBe(1);
    expect(ids(s)).toEqual([1, 2, 3]);
    expect(run(buildItems(s.events())).items).toHaveLength(2);
  });

  it("an older page at the cap is kept; the next auto merge trims back to the cap", () => {
    const s = new TimelineStore();
    s.reset({ events: span(1001, MAX_EVENTS), has_older: true, newest_id: 1000 + MAX_EVENTS });
    s.merge("older", [text(999, "a"), text(1000, "b")], true);
    expect(s.oldestId()).toBe(999);
    expect(s.events()).toHaveLength(MAX_EVENTS + 2);
    s.merge("auto", [text(1001 + MAX_EVENTS, "new")]);
    expect(s.events()).toHaveLength(MAX_EVENTS);
    expect(s.oldestId()).toBe(1002);
  });

  it("drops the oldest events past the cap and says there is more", () => {
    const s = new TimelineStore();
    s.merge("auto", span(1, MAX_EVENTS + 3));
    expect(s.events()).toHaveLength(MAX_EVENTS);
    expect(s.oldestId()).toBe(4);
    expect(s.hasOlder).toBe(true);
  });

  it("a replay of trimmed ids while following keeps the store at the cap", () => {
    const s = new TimelineStore();
    s.merge("auto", span(1, MAX_EVENTS + 3));
    s.merge("auto", span(1, 3));
    expect(s.events()).toHaveLength(MAX_EVENTS);
    expect(s.oldestId()).toBe(4);
  });

  it("scrolled up, live runs are counted, not held: the store size stays", () => {
    const s = new TimelineStore();
    s.merge("auto", span(1, 10));
    s.follow(false);
    for (let r = 0; r < 3; r++) s.merge("auto", span(11 + r * MAX_EVENTS, MAX_EVENTS));
    s.merge("auto", span(11, 5)); // a reconnect page carries counted ids again
    expect(ids(s)).toEqual(span(1, 10).map((e) => e.id));
    expect(s.unseen).toBe(3 * MAX_EVENTS);
    expect(s.behind).toBe(true);
  });

  it("a reset holds the newest page and the held events newer than it, and follows again", () => {
    const s = new TimelineStore();
    s.merge("auto", span(1, 10));
    s.follow(false);
    s.merge("auto", span(11, 2));
    s.merge("older", [text(0, "old")], false);
    s.follow(true);
    s.merge("auto", span(30, 1)); // streamed after the page was read
    s.reset({ events: span(20, 10), has_older: true, newest_id: 29 });
    expect(ids(s)).toEqual(span(20, 11).map((e) => e.id));
    expect(s.hasOlder).toBe(true);
    expect(s.unseen).toBe(0);
    expect(s.behind).toBe(false);
  });
});

describe("buildItems", () => {
  it("pairs tool.started and tool.completed by call_id; a completed-only call renders alone", () => {
    const items = run(buildItems([
      ev(1, "tool.started", "e", { tool_name: "read", data: { call_id: "c1", input: { p: 1 } } }),
      ev(2, "tool.started", "e", { tool_name: "grep", data: { call_id: "c2" } }),
      ev(3, "tool.completed", "e", { tool_name: null, data: { call_id: "c1", output: "ok", is_error: false } }),
      ev(4, "tool.completed", "e", { tool_name: "ls", data: { call_id: "c3", is_error: true } }),
    ])).items;
    expect(items).toEqual([
      { type: "tool", key: "1", name: "read", input: { p: 1 }, output: "ok", done: true, isError: false },
      { type: "tool", key: "2", name: "grep", done: false, isError: false },
      { type: "tool", key: "4", name: "ls", done: true, isError: true },
    ]);
  });

  it("groups each execution under its run, so interleaved runs stay two runs", () => {
    const items = buildItems([
      ev(1, "message.created", null, { content: "go" }),
      ev(2, "run.started", "a", { job_id: 1, prompt: "PROJ-1" }),
      ev(3, "run.started", "b", { job_id: 2 }),
      text(4, "from a", "a"),
      text(5, "from b", "b"),
      ev(6, "run.finished", "a", { outcome: "succeeded" }),
      ev(7, "error", "b", { text: "boom" }),
      ev(8, "job.failed", null, { kind: "harness" }),
      ev(9, "gap", "b", { seq: 3 }),
      ev(10, "tool.called", "b", { tool_name: "jira_get", outcome: "ok" }),
    ]);
    expect(items.map((i) => i.type)).toEqual(["user", "run", "run", "error"]);
    expect(run(items, "a")).toMatchObject({ started: { prompt: "PROJ-1" }, finished: { outcome: "succeeded" } });
    expect(run(items, "a").items.map((i) => i.type)).toEqual(["text"]);
    expect(run(items, "b").items.map((i) => i.type)).toEqual(["text", "error", "marker"]);
  });

  it("renders the legacy assistant.delta as assistant text", () => {
    expect(shown([ev(1, "assistant.delta", "e", { text: "old" })])).toEqual(["text:old"]);
  });

  describe("final answer", () => {
    it("always shows the answer; hides the newest text of its run only when equal (trimmed)", () => {
      expect(shown([text(1, "draft"), text(2, " Done \n"), answer(3, "Done")])).toEqual(["text:draft", "answer:Done"]);
      expect(shown([text(1, "Done"), text(2, "other"), answer(3, "Done")])).toEqual(["text:Done", "text:other", "answer:Done"]);
    });

    it("never hides a text of another run", () => {
      expect(run(buildItems([text(1, "Done", "x"), answer(2, "Done", "e")]), "x").items).toHaveLength(1);
    });

    it("a page with text(B) + message(A\\nB) and an older page with text(A) hide nothing", () => {
      const s = new TimelineStore();
      s.merge("auto", [text(5, "B"), answer(6, "A\nB")]);
      expect(shown(s.events())).toEqual(["text:B", "answer:A\nB"]);
      s.merge("older", [text(4, "A")]);
      expect(shown(s.events())).toEqual(["text:A", "text:B", "answer:A\nB"]);
    });

    it("an older page with a gap, a truncated marker or an equal text changes nothing", () => {
      const s = new TimelineStore();
      s.merge("auto", [text(5, "X"), answer(6, "X")]);
      expect(shown(s.events())).toEqual(["answer:X"]);
      s.merge("older", [ev(2, "gap", "e"), ev(3, "truncated", "e"), text(4, "X")]);
      expect(shown(s.events())).toEqual(["marker", "marker", "text:X", "answer:X"]);
    });
  });
});
