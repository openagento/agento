import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { endSession, getLocale, getUser, login } from "@agento/api";
import {
  CATALOG_DEADLINE_MS, ensureCatalog, forgetFailedSwitch, getCatalog, getLanguageSwitch, subscribeCatalog, switchLanguage,
} from "./catalogStore";
import { sessionBody, sessionLocale, servedCatalog, stalledBody, UNREADABLE_CATALOGS } from "./routes/admin/testing";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const catalogCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls.map(([url]) => new URL(String(url), window.location.origin).pathname).filter((p) => p.startsWith("/api/panel/i18n/"));

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => { fetchMock = vi.fn(async () => json(servedCatalog("pl"))); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { endSession(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("ensureCatalog", () => {
  it("loads the catalog of the session's language once per (language, version)", async () => {
    await ensureCatalog(sessionLocale("pl"));
    expect(getCatalog().locale).toBe("pl");
    expect(getCatalog().messages.panel["nav.jobs"]).toBe("Zadania");
    await ensureCatalog(sessionLocale("pl"));
    expect(catalogCalls(fetchMock)).toEqual(["/api/panel/i18n/pl"]);
  });

  it("loads again when the version of the same language changed", async () => {
    await ensureCatalog(sessionLocale("pl"));
    fetchMock.mockResolvedValueOnce(json(servedCatalog("pl", "v2")));
    await ensureCatalog({ ...sessionLocale("pl"), version: "v2" });
    expect(catalogCalls(fetchMock)).toHaveLength(2);
    expect(getCatalog().version).toBe("v2");
  });

  it("makes no request for a session with no language data", async () => {
    await ensureCatalog(null);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(getCatalog().locale).toBe("en");
  });

  it.each([
    ["a refused read", () => Promise.resolve(json({ error: "nope", code: "NOT_FOUND" }, 404))],
    ["a network failure", () => Promise.reject(new TypeError("offline"))],
  ])("%s shows the bundled English and tells the subscribers", async (_name, answer) => {
    await ensureCatalog(sessionLocale("pl"));
    const told = vi.fn();
    const off = subscribeCatalog(told);
    fetchMock.mockImplementationOnce(answer);
    await ensureCatalog({ ...sessionLocale("pl"), version: "v2" });
    off();
    expect(getCatalog()).toMatchObject({ locale: "en", version: null, messages: {} });
    expect(told).toHaveBeenCalled();
  });

  it.each(UNREADABLE_CATALOGS)("an answer that is not a catalog (%s) shows the bundled English", async (_name, answer) => {
    await ensureCatalog(sessionLocale("pl"));
    fetchMock.mockImplementationOnce(async () => answer());
    await ensureCatalog({ ...sessionLocale("pl"), version: "v2" });
    expect(getCatalog()).toMatchObject({ locale: "en", version: null, messages: {} });
  });

  it("an answer whose body never ends shows the bundled English at the deadline", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementationOnce(async (_url: string, init: RequestInit) => stalledBody(init));
    const done = ensureCatalog(sessionLocale("pl"));
    await vi.advanceTimersByTimeAsync(CATALOG_DEADLINE_MS);
    await done;
    expect(getCatalog()).toMatchObject({ locale: "en", version: null });
  });

  it("gives up after the deadline and shows the bundled English", async () => {
    await ensureCatalog(sessionLocale("pl"));
    vi.useFakeTimers();
    fetchMock.mockImplementationOnce((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }));
    const done = ensureCatalog({ ...sessionLocale("pl"), version: "v2" });
    await vi.advanceTimersByTimeAsync(CATALOG_DEADLINE_MS - 1);
    expect(getCatalog().locale).toBe("pl");
    await vi.advanceTimersByTimeAsync(1);
    await done;
    expect(getCatalog()).toMatchObject({ locale: "en", version: null });
  });
});

describe("switchLanguage", () => {
  const saved = (tag: string, version = "v1") => json({ ui_locale: tag, locale: { ...sessionLocale(tag), version } });
  const signedIn = async (versionOf: Record<string, string> = {}) => {
    routed(versionOf);
    await login("ann", "pw");
  };
  const routed = (versionOf: Record<string, string>) => fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const tag = new URL(String(url), window.location.origin).pathname.split("/").pop()!;
    if (tag === "session") return json(sessionBody({ ui_locale: null, locale: sessionLocale("en") }));
    if (init?.method === "PATCH") {
      const picked = JSON.parse(String(init.body)).ui_locale as string;
      return saved(picked, versionOf[picked]);
    }
    const version = versionOf[tag] ?? "v1";
    const held = (init?.headers as Record<string, string>)["If-None-Match"];
    return held === `"${version}"` ? new Response(null, { status: 304 }) : json(servedCatalog(tag, version));
  });
  const bodiesRead = async () => {
    const answers = await Promise.all(fetchMock.mock.results.map((r) => r.value as Promise<Response>));
    return answers.filter((r, i) => r.status === 200 && String(fetchMock.mock.calls[i][0]).includes("/api/panel/i18n/")).length;
  };
  const saves = () => fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");
  const neverAnswers = (_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });

  it("reads each language once over pl, en and pl again: the way back only asks if the version changed", async () => {
    await signedIn();
    await switchLanguage("pl");
    await switchLanguage("en");
    await switchLanguage("pl");
    expect(catalogCalls(fetchMock)).toEqual(["/api/panel/i18n/pl", "/api/panel/i18n/en", "/api/panel/i18n/pl"]);
    expect(await bodiesRead()).toBe(2);
    expect(getCatalog()).toMatchObject({ locale: "pl", version: "v1" });
    expect(getCatalog().messages.panel["nav.jobs"]).toBe("Zadania");
  });

  it("shows the new version of a language whose catalog changed since the copy", async () => {
    const versions = { pl: "v1" };
    await signedIn(versions);
    await switchLanguage("pl");
    await switchLanguage("en");
    versions.pl = "v2";
    await switchLanguage("pl");
    expect(await bodiesRead()).toBe(3);
    expect(getCatalog().version).toBe("v2");
  });

  it("a switch to a held language whose version check times out saves nothing and keeps what is shown", async () => {
    vi.useFakeTimers();
    await signedIn();
    await switchLanguage("pl");
    await switchLanguage("en");
    const savesBefore = saves().length;
    fetchMock.mockImplementation(neverAnswers);
    const refused = expect(switchLanguage("pl")).resolves.toBe(false);
    await vi.advanceTimersByTimeAsync(CATALOG_DEADLINE_MS);
    await refused;
    expect(saves()).toHaveLength(savesBefore);
    expect(getCatalog()).toMatchObject({ locale: "en", version: "v1" });
    expect(getUser()?.ui_locale).toBe("en");
    expect(getLocale()?.effective).toBe("en");
  });

  const unchanged = (tag: string) => {
    expect(getLanguageSwitch()).toMatchObject({ pending: null, failed: { tag } });
    expect(getCatalog()).toMatchObject({ locale: "en", version: "v1" });
    expect([getUser()?.ui_locale, getLocale()?.effective]).toEqual([null, "en"]);
  };
  const catalogAnswers = (answer: (init: RequestInit) => Response) => fetchMock.mockImplementation(
    async (_url: string, init: RequestInit) => (init.method === "PATCH" ? saved("pl") : answer(init)));

  it.each(UNREADABLE_CATALOGS)("a first read that is not a catalog (%s) sends no save and keeps the language", async (_name, answer) => {
    await signedIn();
    await ensureCatalog(getLocale());
    catalogAnswers(answer);
    await expect(switchLanguage("pl")).resolves.toBe(false);
    expect(saves()).toEqual([]);
    unchanged("pl");
  });

  it.each(UNREADABLE_CATALOGS)("a version check that is not a catalog (%s) sends no save and keeps the language", async (_name, answer) => {
    await signedIn();
    await switchLanguage("pl");
    await switchLanguage("en");
    const savesBefore = saves().length;
    catalogAnswers(answer);
    await expect(switchLanguage("pl")).resolves.toBe(false);
    expect(saves()).toHaveLength(savesBefore);
    expect(getLanguageSwitch()).toMatchObject({ pending: null, failed: { tag: "pl" } });
    expect(getCatalog().locale).toBe("en");
  });

  it("a read whose body never ends sends no save and keeps the language", async () => {
    await signedIn();
    await ensureCatalog(getLocale());
    vi.useFakeTimers();
    catalogAnswers(stalledBody);
    const refused = expect(switchLanguage("pl")).resolves.toBe(false);
    await vi.advanceTimersByTimeAsync(CATALOG_DEADLINE_MS);
    await refused;
    expect(saves()).toEqual([]);
    unchanged("pl");
  });

  it.each([
    ["a body that is not JSON", () => new Response("<html>", { status: 200 })],
    ["null", () => json(null)],
    ["no locale block", () => json({ ui_locale: "pl" })],
    ["a ui_locale that is a number", () => json({ ui_locale: 1, locale: sessionLocale("pl") })],
    ["a locale block with no version", () => json({ ui_locale: "pl", locale: { effective: "pl", instance: "en", available: [] } })],
    ["a list of languages that is a string", () => json({ ui_locale: "pl", locale: { ...sessionLocale("pl"), available: "pl" } })],
    ["a language with no name", () => json({ ui_locale: "pl", locale: { ...sessionLocale("pl"), available: [{ tag: "pl" }] } })],
    ["a stored language that is not a tag", () => json({ ui_locale: "x_y", locale: sessionLocale("pl") })],
    ["an effective language that is not a tag", () => json({ ui_locale: "pl", locale: sessionLocale("x_y") })],
  ])("a save answer that is not readable (%s) keeps the language and is kept as failed", async (_name, answer) => {
    await signedIn();
    await ensureCatalog(getLocale());
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => (init?.method === "PATCH" ? answer() : json(servedCatalog("pl"))));
    await expect(switchLanguage("pl")).resolves.toBe(false);
    unchanged("pl");
  });

  it("a failed first read sends no save and keeps what is shown", async () => {
    await signedIn();
    fetchMock.mockImplementation(async () => json({ error: "nope", code: "NOT_FOUND" }, 404));
    await expect(switchLanguage("pl")).resolves.toBe(false);
    expect(saves()).toEqual([]);
    expect(getCatalog().locale).toBe("en");
  });

  it("a refused save keeps what is shown", async () => {
    await signedIn();
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      (init?.method === "PATCH" ? json({ error: "nope", code: "LOCALE_UNSUPPORTED" }, 400) : json(servedCatalog("pl"))));
    await expect(switchLanguage("pl")).resolves.toBe(false);
    expect(getCatalog().locale).toBe("en");
    expect(getLanguageSwitch()).toMatchObject({ pending: null, failed: { tag: "pl", error: { status: 400 } } });
    forgetFailedSwitch();
    expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });
  });

  describe("a catalog that changes between the read and the save (a `web` restart)", () => {
    const changed = () => json({ error: "server words", code: "CATALOG_CHANGED" }, 409);
    const restarted = (refusals: number, served = ["A", "B"]) => {
      const reads: (string | undefined)[] = [];
      const sent: unknown[] = [];
      fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
        if (init?.method === "PATCH") {
          const version = (init.headers as Record<string, string>)["If-Match"];
          sent.push([JSON.parse(String(init.body)), version]);
          return sent.length <= refusals ? changed() : saved("pl", version.slice(1, -1));
        }
        reads.push((init?.headers as Record<string, string>)["If-None-Match"]);
        return json(servedCatalog("pl", served[Math.min(reads.length, served.length) - 1]));
      });
      return { reads, sent };
    };

    it("reads again once and saves with the new version", async () => {
      await signedIn();
      const { reads, sent } = restarted(1);
      await expect(switchLanguage("pl")).resolves.toBe(true);
      expect(sent).toEqual([[{ ui_locale: "pl" }, '"A"'], [{ ui_locale: "pl" }, '"B"']]);
      expect(reads).toEqual([undefined, undefined]);
      expect(getCatalog()).toMatchObject({ locale: "pl", version: "B" });
      expect(getLocale()).toMatchObject({ effective: "pl", version: "B" });
    });

    it("a second refusal fails the change: no session change, the screen as it was, no held copy, and a retry works", async () => {
      await signedIn();
      const { reads, sent } = restarted(2, ["A", "B", "C"]);
      await expect(switchLanguage("pl")).resolves.toBe(false);
      expect(sent).toHaveLength(2);
      expect(getLanguageSwitch()).toMatchObject({ pending: null, failed: { tag: "pl", error: { status: 409 } } });
      expect(getCatalog()).toMatchObject({ locale: "en", version: null });
      expect([getUser()?.ui_locale, getLocale()?.effective]).toEqual([null, "en"]);

      await expect(switchLanguage("pl")).resolves.toBe(true);
      expect(reads).toEqual([undefined, undefined, undefined]);
      expect(sent[2]).toEqual([{ ui_locale: "pl" }, '"C"']);
      expect(getCatalog()).toMatchObject({ locale: "pl", version: "C" });
      expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });
    });
  });

  it("a save that the server answers with another language shows that language, not the one asked for", async () => {
    await signedIn();
    await switchLanguage("pl");
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => (init?.method === "PATCH"
      ? json({ ui_locale: "de", locale: sessionLocale("pl") })
      : json(servedCatalog(String(url).endsWith("/de") ? "en" : "pl"))));
    await expect(switchLanguage("de")).resolves.toBe(false);
    const save = saves().at(-1)![1]!;
    expect([JSON.parse(String(save.body)), (save.headers as Record<string, string>)["If-Match"]]).toEqual([{ ui_locale: "de" }, '"v1"']);
    expect(getCatalog()).toMatchObject({ locale: "pl", version: "v1" });
    expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });
    expect(getUser()?.ui_locale).toBe("de");
  });

  it("runs one change at a time: a second call during a pending one starts nothing", async () => {
    await signedIn();
    let answer: (r: Response) => void = () => undefined;
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => (init?.method === "PATCH"
      ? new Promise<Response>((resolve) => { answer = resolve; })
      : Promise.resolve(json(servedCatalog("pl")))));
    const seen: (string | null)[] = [];
    const stop = subscribeCatalog(() => seen.push(getLanguageSwitch().pending));
    const first = switchLanguage("pl");
    await vi.waitFor(() => expect(saves()).toHaveLength(1));
    expect(getLanguageSwitch()).toEqual({ pending: "pl", failed: null });

    await expect(switchLanguage("en")).resolves.toBe(false);
    expect(saves()).toHaveLength(1);
    expect(getLanguageSwitch().pending).toBe("pl");

    answer(saved("pl"));
    await expect(first).resolves.toBe(true);
    stop();
    expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });
    expect(getCatalog().locale).toBe("pl");
    expect(seen).toEqual(["pl", null]);
  });

  it("a save that gets no answer keeps the change pending for any time: no second change starts, and the late answer is applied", async () => {
    await signedIn();
    vi.useFakeTimers();
    let answer: (r: Response) => void = () => undefined;
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => (init?.method === "PATCH"
      ? new Promise<Response>((resolve) => { answer = resolve; })
      : Promise.resolve(json(servedCatalog("pl")))));
    const first = switchLanguage("pl");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(saves()).toHaveLength(1);
    expect(saves()[0][1].signal?.aborted ?? false).toBe(false);
    expect(getLanguageSwitch()).toEqual({ pending: "pl", failed: null });
    const sentBefore = fetchMock.mock.calls.length;
    await expect(switchLanguage("en")).resolves.toBe(false);
    expect(fetchMock.mock.calls).toHaveLength(sentBefore);

    answer(saved("pl"));
    await expect(first).resolves.toBe(true);
    expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });
    expect([getUser()?.ui_locale, getLocale()?.effective, getCatalog().locale]).toEqual(["pl", "pl", "pl"]);
  });

  it("a change that an ended session started shows nothing and leaves no pending state", async () => {
    await signedIn();
    let answer: (r: Response) => void = () => undefined;
    fetchMock.mockImplementation(() => new Promise<Response>((resolve) => { answer = resolve; }));
    const late = switchLanguage("pl");
    expect(getLanguageSwitch().pending).toBe("pl");
    endSession();
    expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });
    answer(json(servedCatalog("pl")));
    await expect(late).resolves.toBe(false);
    expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });
    expect(getCatalog().locale).toBe("en");
  });
});

describe("the end of a session", () => {
  const signedInPolish = async () => {
    fetchMock.mockImplementation(async (url: string) => (String(url).endsWith("/api/session")
      ? json(sessionBody({ ui_locale: "pl", locale: sessionLocale("pl") })) : json(servedCatalog("pl"))));
    await login("ann", "pw");
  };

  it("drops the catalog that is shown and every copy", async () => {
    await signedInPolish();
    await ensureCatalog(sessionLocale("pl"));
    const told = vi.fn();
    const off = subscribeCatalog(told);
    endSession();
    off();
    expect(getCatalog()).toMatchObject({ locale: "en", version: null, messages: {} });
    expect(told).toHaveBeenCalled();
    await ensureCatalog(sessionLocale("pl"));
    expect(catalogCalls(fetchMock)).toEqual(["/api/panel/i18n/pl", "/api/panel/i18n/pl"]);
  });

  it("a read that the ended session started shows nothing and leaves no copy", async () => {
    await signedInPolish();
    let answer: (r: Response) => void = () => {};
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { answer = resolve; }));
    const pending = ensureCatalog(sessionLocale("pl"));
    endSession();
    answer(json(servedCatalog("pl")));
    await pending;
    expect(getCatalog()).toMatchObject({ locale: "en", version: null, messages: {} });
    await ensureCatalog(sessionLocale("pl"));
    expect(catalogCalls(fetchMock)).toEqual(["/api/panel/i18n/pl", "/api/panel/i18n/pl"]);
  });

  const deferredFetch = () => {
    let answer: (r: Response) => void = () => undefined;
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { answer = resolve; }));
    return (r: Response) => answer(r);
  };
  const signIn = async () => {
    fetchMock.mockResolvedValueOnce(json(sessionBody({ ui_locale: "pl", locale: sessionLocale("pl") })));
    await login("ann", "pw");
  };
  const settled = () => new Promise<void>((done) => { setTimeout(done, 0); });

  it("a session that ends right after a catalog GET answered leaves no copy: the next session reads again", async () => {
    await signIn();
    const answer = deferredFetch();
    const late = ensureCatalog(sessionLocale("pl"));
    answer(json(servedCatalog("pl")));
    endSession();
    await late;
    expect(getCatalog().version).toBeNull();

    await signIn();
    await ensureCatalog(sessionLocale("pl"));
    expect(catalogCalls(fetchMock)).toEqual(["/api/panel/i18n/pl", "/api/panel/i18n/pl"]);
  });

  it("a session that ends right after the save answered 409 starts no second read and no second save", async () => {
    await signIn();
    fetchMock.mockResolvedValueOnce(json(servedCatalog("en")));
    const answer = deferredFetch();
    const late = switchLanguage("en");
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(1));
    const requests = fetchMock.mock.calls.length;
    answer(json({ error: "server words", code: "CATALOG_CHANGED" }, 409));
    endSession();
    await expect(late).resolves.toBe(false);
    await settled();
    expect(fetchMock.mock.calls).toHaveLength(requests);
    expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });
    expect(getCatalog().version).toBeNull();
  });

  /** An answer whose body needs no macrotask, so the walk counts every step of the real stack. */
  const quick = (body: unknown) =>
    ({ ok: true, status: 200, statusText: "", headers: new Headers(), json: () => Promise.resolve(body) }) as unknown as Response;
  const TICKS = 24;
  const EACH_TICK = Array.from({ length: TICKS + 1 }, (_unused, ticks) => ticks);
  const tick = async (count: number) => { for (let i = 0; i < count; i += 1) await Promise.resolve(); };
  const patches = () => fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");
  const savingEnglish = async () => {
    await signIn();
    fetchMock.mockResolvedValueOnce(quick(servedCatalog("en")));
    const answer = deferredFetch();
    const late = switchLanguage("en");
    await vi.waitFor(() => expect(patches()).toHaveLength(1));
    return { late, answer: () => answer(quick({ ui_locale: "en", locale: sessionLocale("en") })) };
  };

  it("the walk below covers a read and a save to their ends", async () => {
    await signIn();
    const answer = deferredFetch();
    void ensureCatalog(sessionLocale("pl"));
    answer(quick(servedCatalog("pl")));
    await tick(TICKS);
    expect(getCatalog()).toMatchObject({ locale: "pl", version: "v1" });
    endSession();

    const save = await savingEnglish();
    save.answer();
    await tick(TICKS);
    expect([getCatalog().locale, getCatalog().version, getLocale()?.effective]).toEqual(["en", "v1", "en"]);
  });

  it.each(EACH_TICK)("a session that ends %i microtasks after the catalog GET answered shows nothing", async (ticks) => {
    await signIn();
    const answer = deferredFetch();
    const late = ensureCatalog(sessionLocale("pl"));
    answer(quick(servedCatalog("pl")));
    await tick(ticks);
    endSession();
    await late;
    await settled();
    expect(getCatalog().version).toBeNull();
  });

  it.each(EACH_TICK)("a session that ends %i microtasks after the save answered keeps no session data, shows nothing and sends nothing", async (ticks) => {
    const save = await savingEnglish();
    const requests = fetchMock.mock.calls.length;
    save.answer();
    await tick(ticks);
    endSession();
    await save.late;
    await settled();
    expect(fetchMock.mock.calls).toHaveLength(requests);
    expect([getUser(), getLocale()]).toEqual([null, null]);
    expect(getCatalog().version).toBeNull();
    expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });
  });

});
