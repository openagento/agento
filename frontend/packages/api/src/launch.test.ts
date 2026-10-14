import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const bridge = vi.hoisted(() => ({ create: vi.fn(), close: vi.fn() }));
vi.mock("@agento/miniapp-bridge", () => ({
  createLaunchBridge: (o: unknown) => { bridge.create(o); return { close: bridge.close }; },
}));

import { endSession, login } from "./session";
import { queryClient } from "./query";
import { ACTION_DENIED, LAUNCH_ENDED, openLaunch, reconcileLaunches, trackedLaunchIds } from "./launch";

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
    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toMatch(/\/api\/miniapps\/launches$/);
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

describe("a preview launch", () => {
  it("asks for a preview and never reuses the window of an approved launch of the same app", async () => {
    const popup = { close: vi.fn(), focus: vi.fn(), closed: false };
    const { w } = fakeWindow(popup);
    fetchMock.mockResolvedValueOnce(json(200, created));
    await openLaunch({ agentViewId: 1, artifactCode: "app" }, new Set(), w);
    fetchMock.mockResolvedValueOnce(json(200, { ...created, launch_id: "L2" }));
    const out = await openLaunch({ agentViewId: 1, artifactCode: "app", preview: true }, new Set(["L1"]), w);
    expect(out).toMatchObject({ kind: "opened", launch_id: "L2" });
    expect(new URL(String(fetchMock.mock.calls.at(-1)?.[0])).pathname).toBe("/api/miniapps/previews");
    expect(JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body)))
      .toEqual({ agent_view_id: 1, artifact_code: "app" });
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

describe("a failed action", () => {
  it("always answers {ok: false, error: {code, message}} with a message a user can act on", async () => {
    const { w } = fakeWindow({ close: vi.fn(), focus: vi.fn(), closed: false });
    fetchMock.mockResolvedValueOnce(json(200, created));
    await openLaunch({ agentViewId: 1, artifactCode: "app" }, new Set(), w);
    const onAction = bridge.create.mock.calls[0][0].onAction;
    const call = async (status: number, body: unknown) => {
      fetchMock.mockResolvedValueOnce(json(status, body));
      return onAction("jira_add_comment", {});
    };
    // The toolbox refused the capability: its own text says nothing to a user.
    expect(await call(403, { error: "invalid capability" })).toEqual(
      { status: 403, body: { ok: false, error: { code: "forbidden", message: ACTION_DENIED } } });
    // The web's bare "not found": the launch ended.
    expect((await call(404, { ok: false, error: { code: "not_found", message: "not found" } })).body)
      .toEqual({ ok: false, error: { code: "not_found", message: LAUNCH_ENDED } });
    // A reason the toolbox or the web already gives is kept as it is.
    const own = { ok: false, error: { code: "unauthorized", message: "tool x is outside the launch's tool ceiling" } };
    expect(await call(403, own)).toEqual({ status: 403, body: own });
    expect((await call(400, { error: "arguments must be an object" })).body)
      .toEqual({ ok: false, error: { code: "failed", message: "arguments must be an object" } });
    expect(await call(200, { ok: true, result: {} })).toEqual({ status: 200, body: { ok: true, result: {} } });
  });
});
