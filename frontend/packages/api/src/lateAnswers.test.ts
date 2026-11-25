// An answer can come back while its session ends (SEC-7). `apiFetch` drops an answer that it
// sees after the end, but its caller runs some microtasks later. Two guards: the source scan
// proves that each write of module state that follows an `await` has the session check in the
// same synchronous run, and the walk ends the session at each microtask after an answer.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const bridge = vi.hoisted(() => ({ close: vi.fn() }));
vi.mock("@agento/miniapp-bridge", () => ({ createLaunchBridge: () => ({ close: bridge.close }) }));

import { boot, endSession, getCsrf, getLocale, getUser, login, logout, saveLocale, LOGOUT_DEADLINE_MS } from "./session";
import { openLaunch, trackedLaunchIds } from "./launch";

/** An answer whose body needs no macrotask, so the walk counts every step of the caller. */
const answerOf = (status: number, body: unknown) =>
  ({ ok: status < 400, status, statusText: "", headers: new Headers(), json: () => Promise.resolve(body) }) as unknown as Response;
const locale = (effective: string) => ({ effective, instance: "en", available: [], version: "v1" });
const sessionOf = (id: number) =>
  ({ user: { id, username: `u${id}`, role: "user", is_active: true, acl: [], ui_locale: null }, csrf_token: `tok-${id}`, expires_at: "x", locale: locale("en") });
const created = { launch_id: "L1", artifact_code: "app", version_id: "v1", redeem: { url: "https://apps.localhost/_launch", fields: { code: "c" } } };

const TICKS = 12;
const EACH_TICK = Array.from({ length: TICKS + 1 }, (_unused, ticks) => ticks);
const tick = async (count: number) => { for (let i = 0; i < count; i += 1) await Promise.resolve(); };
const flush = () => new Promise<void>((done) => { setTimeout(done, 0); });

let fetchMock: ReturnType<typeof vi.fn>;
let answer: (r: Response) => void;
const deferred = () => fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { answer = resolve; }));
const signIn = async (id: number) => {
  fetchMock.mockResolvedValueOnce(answerOf(200, sessionOf(id)));
  await login(`u${id}`, "pw");
};

beforeEach(() => { fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock); endSession(); });
afterEach(() => { vi.useRealTimers(); endSession(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const nothingOfTheSession = () => expect([getUser(), getCsrf(), getLocale()]).toEqual([null, null, null]);

describe("a session that ends at each microtask after an answer", () => {
  it("the walk covers each operation to its end", async () => {
    const settledWithin = async (start: () => Promise<unknown>, body: unknown) => {
      deferred();
      let ended = false;
      void start().then(() => { ended = true; });
      answer(answerOf(200, body));
      await tick(TICKS);
      expect(ended).toBe(true);
      endSession();
    };
    await settledWithin(boot, sessionOf(1));
    await signIn(1);
    await settledWithin(() => saveLocale("pl", "v1"), { ui_locale: "pl", locale: locale("pl") });
    await signIn(1);
    await settledWithin(logout, undefined);
    await signIn(1);
    const w = { open: () => ({ close: vi.fn(), closed: false }), document } as unknown as Window;
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => undefined);
    await settledWithin(() => openLaunch({ agentViewId: 1, artifactCode: "app" }, new Set(), w), created);
  });

  it.each(EACH_TICK)("boot: an answer of the ended session is not adopted (%i)", async (ticks) => {
    deferred();
    const late = boot().catch(() => null);
    answer(answerOf(200, sessionOf(1)));
    await tick(ticks);
    endSession();
    await late;
    nothingOfTheSession();
  });

  it.each(EACH_TICK)("saveLocale: an answer of the ended session changes no session (%i)", async (ticks) => {
    await signIn(1);
    deferred();
    const late = saveLocale("pl", "v1").catch(() => null);
    answer(answerOf(200, { ui_locale: "pl", locale: locale("pl") }));
    await tick(ticks);
    endSession();
    await late;
    await flush();
    nothingOfTheSession();
  });

  it.each(EACH_TICK)("logout: it ends its own session only, never the next one (%i)", async (ticks) => {
    await signIn(1);
    deferred();
    const late = logout().catch(() => null);
    answer(answerOf(204, undefined));
    await tick(ticks);
    fetchMock.mockResolvedValueOnce(answerOf(200, sessionOf(2)));
    await expect(login("u2", "pw")).resolves.toMatchObject({ id: 2 });
    await late;
    await flush();
    expect([getUser()?.id, getCsrf()]).toEqual([2, "tok-2"]);
  });

  it.each(EACH_TICK)("openLaunch: a launch of the ended session is not tracked, gets no form and loses its window (%i)", async (ticks) => {
    await signIn(1);
    const popup = { close: vi.fn(), closed: false };
    const submit = vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => undefined);
    deferred();
    const late = openLaunch({ agentViewId: 1, artifactCode: "app" }, new Set(), { open: () => popup, document } as unknown as Window)
      .catch(() => null);
    answer(answerOf(200, created));
    await tick(ticks);
    const submitted = submit.mock.calls.length;
    endSession();
    await late;
    await flush();
    expect(trackedLaunchIds()).toEqual([]);
    expect(submit.mock.calls).toHaveLength(submitted);
    expect(popup.close).toHaveBeenCalled();
  });
});

describe("a sign-in while a logout is pending", () => {
  // The logout answer clears the session cookie; the sign-in answer sets it. The browser applies
  // them in the order of arrival, so a logout answer after the sign-in answer signs the new
  // account out. The jar below does what the browser does.
  interface Sent { method: string; arrive: () => void; fail: () => void; answer: (response: Response) => void; pending: boolean; signal: AbortSignal | null }
  const network = () => {
    const jar: { session: string | null } = { session: "of-1" };
    const sent: Sent[] = [];
    fetchMock.mockImplementation((_url: string, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
      const method = init?.method ?? "GET";
      const request: Sent = {
        method, pending: true, signal: init?.signal ?? null,
        arrive: () => {
          if (!request.pending) return;
          request.pending = false;
          if (method === "GET") return resolve(jar.session === "of-1" ? answerOf(200, sessionOf(1)) : answerOf(401, { error: "not signed in" }));
          jar.session = method === "DELETE" ? null : "of-2";
          resolve(method === "DELETE" ? answerOf(204, undefined) : answerOf(200, sessionOf(2)));
        },
        fail: () => { request.pending = false; reject(new TypeError("offline")); },
        answer: (response: Response) => { request.pending = false; resolve(response); },
      };
      init?.signal?.addEventListener("abort", () => { request.pending = false; reject(new DOMException("aborted", "AbortError")); });
      sent.push(request);
    }));
    return { jar, sent };
  };

  it("the sign-in request is not sent before the logout answer", async () => {
    await signIn(1);
    const { sent } = network();
    const leaving = logout().catch(() => null);
    const entering = login("u2", "pw");
    await flush();
    expect(sent.map((r) => r.method)).toEqual(["DELETE"]);
    sent[0].arrive();
    await flush();
    expect(sent.map((r) => r.method)).toEqual(["DELETE", "POST"]);
    sent[1].arrive();
    await expect(entering).resolves.toMatchObject({ id: 2 });
    await leaving;
    expect([getUser()?.id, getCsrf()]).toEqual([2, "tok-2"]);
  });

  it("the cookie of the new sign-in is the last one written, also when the newest request is answered first", async () => {
    await signIn(1);
    const { jar, sent } = network();
    void logout().catch(() => null);
    const entering = login("u2", "pw");
    await flush();
    for (let next = sent.findLast((r) => r.pending); next; next = sent.findLast((r) => r.pending)) {
      next.arrive();
      await flush();
    }
    await entering;
    expect(jar.session).toBe("of-2");
    expect(getUser()?.id).toBe(2);
  });

  it("two logouts are pending and the newer one is answered first: the sign-in waits for both", async () => {
    await signIn(1);
    const { jar, sent } = network();
    void logout().catch(() => null);
    void logout().catch(() => null);
    await flush();
    expect(sent.map((r) => r.method)).toEqual(["DELETE", "DELETE"]);
    sent[1].arrive();
    await flush();
    const entering = login("u2", "pw");
    await flush();
    expect(sent).toHaveLength(2);
    sent[0].arrive();
    await flush();
    expect(sent.map((r) => r.method)).toEqual(["DELETE", "DELETE", "POST"]);
    sent[2].arrive();
    await expect(entering).resolves.toMatchObject({ id: 2 });
    expect(jar.session).toBe("of-2");
  });

  it("a logout that fails (no network) does not hold the sign-in back", async () => {
    await signIn(1);
    const { sent } = network();
    const leaving = logout().catch(() => null);
    const entering = login("u2", "pw");
    await flush();
    sent[0].fail();
    await flush();
    expect(sent.map((r) => r.method)).toEqual(["DELETE", "POST"]);
    sent[1].arrive();
    await expect(entering).resolves.toMatchObject({ id: 2 });
    await leaving;
    expect(getUser()?.id).toBe(2);
  });

  it("a logout that gets no answer is cancelled at its deadline: the sign-in goes out then, and the late answer clears no cookie", async () => {
    await signIn(1);
    vi.useFakeTimers();
    const { jar, sent } = network();
    const leaving = logout().catch(() => null);
    const entering = login("u2", "pw");
    await vi.advanceTimersByTimeAsync(LOGOUT_DEADLINE_MS - 1);
    expect(sent.map((r) => r.method)).toEqual(["DELETE"]);
    expect(sent[0].signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(sent[0].signal?.aborted).toBe(true);
    expect(sent.map((r) => r.method)).toEqual(["DELETE", "POST"]);
    sent[1].arrive();
    await expect(entering).resolves.toMatchObject({ id: 2 });
    sent[0].arrive();
    await leaving;
    expect(jar.session).toBe("of-2");
    expect([getUser()?.id, getCsrf()]).toEqual([2, "tok-2"]);
  });

  it("a logout that gets no answer rejects and keeps the session of the tab: the old cookie still signs in, and the tab agrees", async () => {
    await signIn(1);
    vi.useFakeTimers();
    const { jar, sent } = network();
    const leaving = logout().then(() => "ended", () => "rejected");
    await vi.advanceTimersByTimeAsync(LOGOUT_DEADLINE_MS);
    expect(await leaving).toBe("rejected");
    expect([getUser()?.id, getCsrf()]).toEqual([1, "tok-1"]);
    expect(jar.session).toBe("of-1");
    const reading = boot();
    sent[1].arrive();
    await expect(reading).resolves.toMatchObject({ id: 1 });
  });

  it.each([
    ["no network", (r: Sent) => r.fail()],
    ["a server error", (r: Sent) => r.answer(answerOf(500, { error: "boom" }))],
  ])("a logout that fails (%s) rejects and keeps the session of the tab", async (_name, outcome) => {
    await signIn(1);
    const { sent } = network();
    const leaving = logout().then(() => "ended", () => "rejected");
    await flush();
    outcome(sent[0]);
    expect(await leaving).toBe("rejected");
    expect([getUser()?.id, getCsrf()]).toEqual([1, "tok-1"]);
  });

  it.each([
    ["the server ended the session (204)", 204],
    ["the server says that the session is already gone (401)", 401],
  ])("a logout ends the session of the tab when %s", async (_name, status) => {
    await signIn(1);
    const { sent } = network();
    const leaving = logout();
    await flush();
    sent[0].answer(answerOf(status, status === 204 ? undefined : { error: "not signed in", code: "AUTH_REQUIRED" }));
    await expect(leaving).resolves.toBeUndefined();
    nothingOfTheSession();
  });
});

describe("the source of the api package", () => {
  const SESSION_CHECK = /^(at [!=]== (epoch|getEpoch\(\))|getEpoch\(\) [!=]== at)$/;
  const CHANGES = new Set(["set", "delete", "clear", "add"]);
  type Fn = ts.FunctionDeclaration | ts.ArrowFunction | ts.FunctionExpression | ts.MethodDeclaration;
  const isFn = (node: ts.Node): node is Fn =>
    ts.isFunctionDeclaration(node) || ts.isArrowFunction(node) || ts.isFunctionExpression(node) || ts.isMethodDeclaration(node);
  const isAsync = (fn: Fn) => !!fn.modifiers?.some((m) => m.kind === ts.SyntaxKind.AsyncKeyword);

  function lateWrites(file: string): string[] {
    const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true);
    const state = new Set<string>();
    const functions = new Map<string, Fn>();
    for (const statement of source.statements) {
      if (ts.isFunctionDeclaration(statement) && statement.name) functions.set(statement.name.text, statement);
      if (!ts.isVariableStatement(statement)) continue;
      for (const d of statement.declarationList.declarations) {
        const kept = statement.declarationList.flags & ts.NodeFlags.Let || (d.initializer && ts.isNewExpression(d.initializer));
        if (kept && ts.isIdentifier(d.name)) state.add(d.name.text);
        else if (d.initializer && isFn(d.initializer) && ts.isIdentifier(d.name)) functions.set(d.name.text, d.initializer);
      }
    }
    const writers = new Set<string>();
    const isWrite = (node: ts.Node): boolean => {
      if (ts.isBinaryExpression(node) && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
        && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment) return ts.isIdentifier(node.left) && state.has(node.left.text);
      if (!ts.isCallExpression(node)) return false;
      const callee = node.expression;
      if (ts.isIdentifier(callee)) return writers.has(callee.text);
      return ts.isPropertyAccessExpression(callee) && ts.isIdentifier(callee.expression)
        && state.has(callee.expression.text) && CHANGES.has(callee.name.text);
    };
    /** The writes of `fn` itself: a nested function runs at another time and is read on its own. */
    const writesOf = (fn: Fn): ts.Node[] => {
      const found: ts.Node[] = [];
      const visit = (node: ts.Node): void => {
        if (node !== fn && isFn(node)) return;
        if (isWrite(node)) found.push(node);
        ts.forEachChild(node, visit);
      };
      visit(fn);
      return found;
    };
    for (let grew = true; grew;) {
      grew = false;
      for (const [name, fn] of functions) {
        if (!writers.has(name) && !isAsync(fn) && writesOf(fn).length) { writers.add(name); grew = true; }
      }
    }

    const late: string[] = [];
    const read = (fn: Fn): void => {
      const awaits: ts.Node[] = [];
      const checks: ts.IfStatement[] = [];
      const visit = (node: ts.Node): void => {
        if (node !== fn && isFn(node)) return;
        if (ts.isAwaitExpression(node)) awaits.push(node);
        if (ts.isIfStatement(node) && SESSION_CHECK.test(node.expression.getText(source))) checks.push(node);
        ts.forEachChild(node, visit);
      };
      visit(fn);
      for (const write of writesOf(fn)) {
        const before = awaits.filter((a) => a.end <= write.getStart(source)).at(-1);
        if (!before) continue;
        if (!checks.some((c) => c.getStart(source) >= before.end && c.getStart(source) < write.getStart(source))) {
          late.push(`${file.split("/").pop()}:${source.getLineAndCharacterOfPosition(write.getStart(source)).line + 1}: ${write.getText(source).slice(0, 50)}`);
        }
      }
    };
    const visitAll = (node: ts.Node): void => {
      if (isFn(node) && isAsync(node)) read(node);
      ts.forEachChild(node, visitAll);
    };
    visitAll(source);
    return late;
  }

  it("no async function writes module state after an await with no session check between the await and the write", () => {
    const files = readdirSync(__dirname).filter((f) => /\.tsx?$/.test(f) && !/\.test\./.test(f)).map((f) => join(__dirname, f));
    expect(files.length).toBeGreaterThan(10);
    expect(files.flatMap(lateWrites)).toEqual([]);
  });
});
