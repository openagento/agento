import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as pkg from "./index";
import { apiFetch, sameOriginUrl } from "./apiFetch";
import { ApiError, CsrfMissingError, SessionChangedError } from "./errors";
import { boot, endSession, getCsrf, getDisplay, getUser, login, logout, onEndSession, onExpired } from "./session";
import { queryClient } from "./query";

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const sessionBody = (id: number, csrf: string) =>
  ({ user: { id, username: `u${id}`, role: "user", is_active: true }, csrf_token: csrf, expires_at: "x" });

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  endSession();
});
afterEach(() => vi.unstubAllGlobals());

describe("apiFetch", () => {
  it("refuses a cross-origin or protocol-relative path", () => {
    expect(() => sameOriginUrl("https://evil.example/api")).toThrow();
    expect(() => sameOriginUrl("//evil.example/api")).toThrow();
    expect(sameOriginUrl("/api/x").origin).toBe(window.location.origin);
  });

  it("does not send a write without a CSRF token", async () => {
    await expect(apiFetch("/api/x", { method: "POST", json: {} })).rejects.toBeInstanceOf(CsrfMissingError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends the in-memory token on a write, same-origin credentials", async () => {
    fetchMock.mockResolvedValueOnce(json(200, sessionBody(1, "tok-1")));
    await login("a", "b");
    fetchMock.mockResolvedValueOnce(json(200, { ok: true }));
    await apiFetch("/api/x", { method: "POST", json: { a: 1 } });
    const [, init] = fetchMock.mock.calls[1];
    expect(init.headers["X-CSRF-Token"]).toBe("tok-1");
    expect(init.credentials).toBe("same-origin");
  });

  it("keeps the error body and Retry-After", async () => {
    fetchMock.mockResolvedValueOnce(json(429, { error: "slow down" }, { "Retry-After": "7" }));
    const e = (await apiFetch("/api/x").catch((x) => x)) as ApiError;
    expect(e).toBeInstanceOf(ApiError);
    expect([e.status, e.message, e.retryAfter]).toEqual([429, "slow down", 7]);
  });

  it("the first 401 ends the session once; quiet401 does not", async () => {
    const expired = vi.fn();
    const off = onExpired(expired);
    fetchMock.mockResolvedValue(json(401, { error: "no" }));
    await apiFetch("/api/x", { quiet401: true }).catch(() => {});
    expect(expired).not.toHaveBeenCalled();
    await Promise.all([apiFetch("/api/a").catch(() => {}), apiFetch("/api/b").catch(() => {})]);
    expect(expired).toHaveBeenCalledTimes(1);
    off();
  });

  it("drops a response that arrives after the session changed", async () => {
    let resolve!: (r: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>((r) => { resolve = r; }));
    const pending = apiFetch("/api/x");
    endSession();
    resolve(json(200, { secret: "of A" }));
    await expect(pending).rejects.toBeInstanceOf(SessionChangedError);
  });
});

describe("session", () => {
  it("does not export the CSRF-less login write", () => {
    expect(Object.keys(pkg)).not.toContain("sendLogin");
  });

  it("A → logout → B leaves nothing of A", async () => {
    const teardown = vi.fn();
    onEndSession(teardown);
    fetchMock.mockResolvedValueOnce(json(200, sessionBody(1, "tok-A")));
    await login("a", "pw");
    queryClient.setQueryData(["threads"], [{ id: 1, title: "A's thread" }]);
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await logout();
    expect(getCsrf()).toBeNull();
    expect(getUser()).toBeNull();
    expect(queryClient.getQueryData(["threads"])).toBeUndefined();
    expect(teardown).toHaveBeenCalled();
    fetchMock.mockResolvedValueOnce(json(200, sessionBody(2, "tok-B")));
    await login("b", "pw");
    expect(getCsrf()).toBe("tok-B");
    expect(getUser()?.id).toBe(2);
  });

  it("keeps the display settings of the session and drops them at its end", async () => {
    const display = { date_format: "eu", timezone: "Europe/Warsaw" };
    fetchMock.mockResolvedValueOnce(json(200, { ...sessionBody(1, "tok-D"), display }));
    await boot();
    expect(getDisplay()).toEqual(display);
    endSession();
    expect(getDisplay()).toBeNull();
    fetchMock.mockResolvedValueOnce(json(200, { ...sessionBody(1, "tok-D"), display: null }));
    await login("a", "pw");
    expect(getDisplay()).toBeNull();
  });

  it("stores nothing in browser storage or cookies", async () => {
    fetchMock.mockResolvedValueOnce(json(200, sessionBody(1, "tok-S")));
    await login("a", "pw");
    expect(JSON.stringify({ ...localStorage })).not.toContain("tok-S");
    expect(JSON.stringify({ ...sessionStorage })).not.toContain("tok-S");
    expect(document.cookie).not.toContain("tok-S");
  });
});
