import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const bridge = vi.hoisted(() => ({ create: vi.fn(), close: vi.fn() }));
vi.mock("@agento/miniapp-bridge", () => ({
  createLaunchBridge: (o: unknown) => { bridge.create(o); return { close: bridge.close }; },
}));

import { endSession, login, queryClient } from "@agento/api";
import { openLaunch, reconcileLaunches, trackedLaunchIds } from "./launch";
import { scopeId } from "./routes/Grants";
import { safeNext } from "./safeNext";

const CODE = "exch-code-SECRET";
const created = {
  launch_id: "L1", artifact_code: "app", version_id: "v1",
  redeem: { url: "https://apps.localhost/_launch", fields: { code: CODE } },
};
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });

function fakeWindow(popup: Partial<Window> | null) {
  const submitted: HTMLFormElement[] = [];
  const order: string[] = [];
  bridge.create.mockImplementation(() => order.push("bridge"));
  const doc = document;
  vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function (this: HTMLFormElement) {
    order.push("submit"); submitted.push(this.cloneNode(true) as HTMLFormElement);
  });
  const w = { open: vi.fn(() => popup), document: doc } as unknown as Window;
  return { w, submitted, order };
}

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(async () => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockResolvedValueOnce(json(200, { user: { id: 1 }, csrf_token: "t", expires_at: "x" }));
  await login("a", "b");
  bridge.create.mockReset();
  bridge.close.mockReset();
});
afterEach(() => { endSession(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("openLaunch", () => {
  it("reports blocked and sends no request when the popup is blocked", async () => {
    const { w } = fakeWindow(null);
    expect(await openLaunch({ agentViewId: 1, artifactCode: "app" }, new Set(), w)).toEqual({ kind: "blocked" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("closes the reserved window when the API refuses", async () => {
    const popup = { close: vi.fn(), closed: false };
    const { w } = fakeWindow(popup);
    fetchMock.mockResolvedValueOnce(json(403, { error: "no" }));
    await expect(openLaunch({ agentViewId: 1, artifactCode: "app" }, new Set(), w)).rejects.toThrow();
    expect(popup.close).toHaveBeenCalled();
  });

  it("installs the bridge before the form POST; the code is in no URL, cache or DOM", async () => {
    const popup = { close: vi.fn(), focus: vi.fn(), closed: false };
    const { w, submitted, order } = fakeWindow(popup);
    fetchMock.mockResolvedValueOnce(json(200, created));
    const out = await openLaunch({ agentViewId: 1, artifactCode: "app" }, new Set(), w);
    expect(out).toEqual({ kind: "opened", launch_id: "L1", artifact_code: "app", version_id: "v1" });
    expect(order).toEqual(["bridge", "submit"]);
    const [url, name] = (w.open as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("about:blank");
    expect(name).not.toContain(CODE);
    expect(submitted[0].method).toBe("post");
    expect(submitted[0].action).not.toContain(CODE);
    expect(document.body.innerHTML).not.toContain(CODE);
    expect(JSON.stringify(queryClient.getQueryCache().getAll().map((q) => q.state.data))).not.toContain(CODE);
    expect(JSON.stringify(queryClient.getMutationCache().getAll().map((m) => m.state.data))).not.toContain(CODE);
    expect(window.location.href).not.toContain(CODE);

    // A second click on a live launch focuses it and makes no request.
    const calls = fetchMock.mock.calls.length;
    expect(await openLaunch({ agentViewId: 1, artifactCode: "app" }, new Set(["L1"]), w))
      .toEqual({ kind: "reused", launch_id: "L1" });
    expect(popup.focus).toHaveBeenCalled();
    expect(fetchMock.mock.calls.length).toBe(calls);

    endSession();
    expect(popup.close).toHaveBeenCalled();
    expect(bridge.close).toHaveBeenCalled();
    expect(trackedLaunchIds()).toEqual([]);
  });
});

describe("reconcileLaunches", () => {
  it("closes a launch the server no longer lists, keeps one made after the list was requested", async () => {
    const popup = { close: vi.fn(), focus: vi.fn(), closed: false };
    const { w } = fakeWindow(popup);
    fetchMock.mockResolvedValueOnce(json(200, created));
    const before = Date.now() - 1;
    await openLaunch({ agentViewId: 1, artifactCode: "app" }, new Set(), w);
    reconcileLaunches(new Set(), before);
    expect(trackedLaunchIds()).toEqual(["L1"]);
    reconcileLaunches(new Set(["L1"]), Date.now() + 1);
    expect(trackedLaunchIds()).toEqual(["L1"]);
    reconcileLaunches(new Set(), Date.now() + 1);
    expect(trackedLaunchIds()).toEqual([]);
    expect(popup.close).toHaveBeenCalled();
    expect(bridge.close).toHaveBeenCalled();
  });
});

describe("grant scope ids", () => {
  it.each([["", undefined], [" 7 ", 7], ["abc", null], ["0", null], ["-1", null], ["1.5", null], ["1e3", null],
    ["99999999999999999999", null]])("%j → %j", (v, out) => expect(scopeId(v)).toBe(out));
});

describe("safeNext", () => {
  it.each([
    ["/conversations/3?x=1", "/conversations/3?x=1"], [null, "/"], ["//evil.example", "/"],
    ["https://evil.example/", "/"], ["/\\evil.example", "/"], ["/login?next=/x", "/"], ["javascript:alert(1)", "/"],
  ])("%s → %s", (input, out) => expect(safeNext(input)).toBe(out));
});
