import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as pkg from "./index";
import { isLanguageTag, LANGUAGE_TAG } from "./shape";
import { apiFetch, sameOriginUrl } from "./apiFetch";
import { ApiError, CsrfMissingError, SessionChangedError } from "./errors";
import { boot, endSession, getCsrf, getDisplay, getLocale, getUser, login, logout, onEndSession, onExpired, saveLocale } from "./session";
import { errorCode } from "./errorText";
import { queryClient } from "./query";

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const sessionBody = (id: number, csrf: string) =>
  ({ user: { id, username: `u${id}`, role: "user", is_active: true, ui_locale: null }, csrf_token: csrf, expires_at: "x" });

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

describe("the shape of an answer", () => {
  // A type argument of `apiFetch` and an `as` on a parsed body give a type to data that nobody
  // checked. The readers of the session and of the catalogs take `unknown` and use `shaped`.
  // The three files below read other contracts, not checked today.
  const UNCHECKED = ["jobLinks.tsx", "launch.ts", "useUploads.ts"];
  const sources = readdirSync(__dirname).filter((name) => /\.tsx?$/.test(name) && !/\.test\./.test(name));

  it("no reader gives a type to an answer without a check", () => {
    const typed = sources.filter((name) => /apiFetch<|\.json\(\)[^;]*\bas\b|(?<!\.)\bbody as\b/.test(readFileSync(join(__dirname, name), "utf8")));
    expect(sources).toEqual(expect.arrayContaining(["catalog.ts", "session.ts", ...UNCHECKED]));
    expect(typed.filter((name) => name !== "apiFetch.ts")).toEqual(UNCHECKED);
  });
});

describe("the rules the server has too", () => {
  const python = readFileSync(join(__dirname, "../../../../src/agento/framework/i18n/__init__.py"), "utf8");

  it("a language tag has the pattern of the server's TAG_RE", () => {
    expect(/^TAG_RE = re\.compile\(r"(.+)"\)$/m.exec(python)?.[1]).toBe(LANGUAGE_TAG.source);
  });

  it.each(["pl", "en", "qaa", "zh-Hant", "pt-BR", "es-419", "zh-Hant-TW"])("%s is a language tag", (tag) => {
    expect(isLanguageTag(tag)).toBe(true);
  });

  it.each(["x_y", "PL", "pl-pl", "p", "polski", "", "pl-", "en-US-x", 7, null])("%s is not a language tag", (tag) => {
    expect(isLanguageTag(tag)).toBe(false);
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

  it("keeps the language data of the session and drops it at its end; a session with none holds null", async () => {
    const locale = { effective: "pl", instance: "en", available: [{ tag: "pl", name: "Polski" }], version: "v1" };
    fetchMock.mockResolvedValueOnce(json(200, { ...sessionBody(1, "tok-L"), locale }));
    await boot();
    expect(getLocale()).toEqual(locale);
    endSession();
    expect(getLocale()).toBeNull();
    fetchMock.mockResolvedValueOnce(json(200, sessionBody(1, "tok-L")));
    await login("a", "pw");
    expect(getLocale()).toBeNull();
  });

  const LOCALE = { effective: "pl", instance: "en", available: [{ tag: "pl", name: "Polski" }], version: "v1" };
  const UNREADABLE_SESSIONS: [string, unknown][] = [
    ["null", null],
    ["an array", []],
    ["no user", { csrf_token: "tok" }],
    ["a stored language that is a number", { ...sessionBody(1, "tok"), user: { ...sessionBody(1, "tok").user, ui_locale: 1 } }],
    ["a language block that is a string", { ...sessionBody(1, "tok"), locale: "pl" }],
    ["a language block with no version", { ...sessionBody(1, "tok"), locale: { ...LOCALE, version: undefined } }],
    ["a language block with no effective language", { ...sessionBody(1, "tok"), locale: { ...LOCALE, effective: null } }],
    ["a list of languages that is a string", { ...sessionBody(1, "tok"), locale: { ...LOCALE, available: "pl" } }],
    ["a language that is a string", { ...sessionBody(1, "tok"), locale: { ...LOCALE, available: ["pl"] } }],
    ["a language with no name", { ...sessionBody(1, "tok"), locale: { ...LOCALE, available: [{ tag: "pl" }] } }],
    ["a stored language that is not a tag", { ...sessionBody(1, "tok"), user: { ...sessionBody(1, "tok").user, ui_locale: "x_y" } }],
    ["an effective language that is not a tag", { ...sessionBody(1, "tok"), locale: { ...LOCALE, effective: "x_y" } }],
    ["an instance language that is not a tag", { ...sessionBody(1, "tok"), locale: { ...LOCALE, instance: "PL" } }],
    ["a language of the list that is not a tag", { ...sessionBody(1, "tok"), locale: { ...LOCALE, available: [{ tag: "pl_PL", name: "Polski" }] } }],
  ];

  it.each(UNREADABLE_SESSIONS)("a session answer that is not readable (%s) starts no session: boot and login reject", async (_name, body) => {
    fetchMock.mockResolvedValue(json(200, body));
    await expect(boot()).rejects.toBeInstanceOf(TypeError);
    await expect(login("a", "pw")).rejects.toBeInstanceOf(TypeError);
    expect([getUser(), getCsrf(), getLocale()]).toEqual([null, null, null]);
  });

  it.each([
    ["null", null],
    ["no language block", { ui_locale: "pl" }],
    ["a stored language that is a number", { ui_locale: 1, locale: LOCALE }],
    ["a language block with no version", { ui_locale: "pl", locale: { ...LOCALE, version: undefined } }],
    ["a language with no name", { ui_locale: "pl", locale: { ...LOCALE, available: [{ tag: "pl" }] } }],
    ["a stored language that is not a tag", { ui_locale: "x_y", locale: LOCALE }],
    ["an effective language that is not a tag", { ui_locale: "pl", locale: { ...LOCALE, effective: "x_y" } }],
  ])("a save answer that is not readable (%s) rejects and changes nothing", async (_name, body) => {
    fetchMock.mockResolvedValueOnce(json(200, { ...sessionBody(1, "tok-P"), locale: { ...LOCALE, effective: "en" } }));
    await login("a", "pw");
    fetchMock.mockResolvedValueOnce(json(200, body));
    await expect(saveLocale("pl", "v1")).rejects.toBeInstanceOf(TypeError);
    expect([getUser()?.ui_locale, getLocale()?.effective]).toEqual([null, "en"]);
  });

  it("a refused sign-in keeps the answer's code on its error", async () => {
    fetchMock.mockResolvedValueOnce(json(401, { error: "invalid credentials", code: "AUTH_INVALID_CREDENTIALS" }));
    const e = (await login("a", "wrong").catch((x) => x)) as ApiError;
    expect([e.status, errorCode(e)]).toEqual([401, "AUTH_INVALID_CREDENTIALS"]);
  });

  it("saveLocale PATCHes the tag only, with the version of the held catalog in If-Match, and stores the answer; a refusal changes nothing", async () => {
    const before = { effective: "en", instance: "en", available: [], version: "v1" };
    const after = { ...before, effective: "pl", version: "v2" };
    fetchMock.mockResolvedValueOnce(json(200, { ...sessionBody(1, "tok-P"), locale: before }));
    await login("a", "pw");
    fetchMock.mockResolvedValueOnce(json(200, { ui_locale: "pl", locale: after }));
    await saveLocale("pl", "v2");
    const [url, init] = fetchMock.mock.calls[1];
    expect([new URL(url).pathname, init.method, init.body, init.headers["X-CSRF-Token"], init.headers["If-Match"]])
      .toEqual(["/api/profile/locale", "PATCH", JSON.stringify({ ui_locale: "pl" }), "tok-P", '"v2"']);
    expect(Object.keys(fetchMock.mock.calls[0][1].headers)).not.toContain("If-Match");
    expect([getUser()?.ui_locale, getLocale()]).toEqual(["pl", after]);
    fetchMock.mockResolvedValueOnce(json(400, { error: "unsupported", code: "LOCALE_UNSUPPORTED" }));
    await expect(saveLocale("xx", "v2")).rejects.toBeInstanceOf(ApiError);
    expect([getUser()?.ui_locale, getLocale()]).toEqual(["pl", after]);
  });

  it("stores nothing in browser storage or cookies", async () => {
    fetchMock.mockResolvedValueOnce(json(200, sessionBody(1, "tok-S")));
    await login("a", "pw");
    expect(JSON.stringify({ ...localStorage })).not.toContain("tok-S");
    expect(JSON.stringify({ ...sessionStorage })).not.toContain("tok-S");
    expect(document.cookie).not.toContain("tok-S");
  });
});
