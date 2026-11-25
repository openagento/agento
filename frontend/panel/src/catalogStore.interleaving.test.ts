// The end of a session can come at any moment of a catalog operation. Two guards: the source
// scan proves that an operation touches the shown catalog, the language change and the requests
// only through `live`, and the walk ends the session at each microtask between an answer and
// the next step of each path and proves that the ended operation left nothing: no copy, no
// shown whole catalog, no request.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as Api from "@agento/api";

interface Call { name: string; args: unknown[]; answer: (value: unknown) => void; fail: (error: unknown) => void }
const calls: Call[] = [];
const pending = (name: string) => (...args: unknown[]) => new Promise((answer, fail) => { calls.push({ name, args, answer, fail }); });

vi.mock("@agento/api", async (original) => ({
  ...await original<typeof Api>(),
  fetchCatalog: pending("read"),
  fetchCatalogUnless: pending("read-unless"),
  saveLocale: pending("save"),
}));

const { ApiError, endSession } = await import("@agento/api");
const { ensureCatalog, getCatalog, getLanguageSwitch, switchLanguage } = await import("./catalogStore");

afterEach(() => { endSession(); calls.length = 0; });

const served = (locale: string, version: string) => ({ locale, version, messages: { panel: {} }, fallback: {} });
const session = (effective: string, version: string) => ({ effective, instance: "en", available: [], version });
const changed = () => new ApiError(409, "server words", undefined, { code: "CATALOG_CHANGED" });
const refused = () => new ApiError(400, "server words", undefined, { code: "LOCALE_UNSUPPORTED" });
const offline = () => new TypeError("offline");

type Step = { expect: string } & ({ answer: unknown } | { fail: unknown });
interface Path { name: string; before?: () => Promise<void>; start: () => Promise<unknown>; steps: Step[] }

const held = async () => {
  const first = switchLanguage("pl");
  await settle({ expect: "read", answer: served("pl", "A") });
  await settle({ expect: "save", answer: session("pl", "A") });
  await first;
  calls.length = 0;
};

const flush = () => new Promise<void>((done) => { setTimeout(done, 0); });
const act = (step: Step) => {
  const call = calls.at(-1)!;
  expect(call.name).toBe(step.expect);
  if ("answer" in step) call.answer(step.answer);
  else call.fail(step.fail);
};
async function settle(step: Step) { act(step); await flush(); }

const PATHS: Path[] = [
  { name: "ensureCatalog: the read answers", start: () => ensureCatalog(session("pl", "A")),
    steps: [{ expect: "read", answer: served("pl", "A") }] },
  { name: "ensureCatalog: the read fails", start: () => ensureCatalog(session("pl", "A")),
    steps: [{ expect: "read", fail: offline() }] },
  { name: "switchLanguage: first read, then the save", start: () => switchLanguage("pl"),
    steps: [{ expect: "read", answer: served("pl", "A") }, { expect: "save", answer: session("pl", "A") }] },
  { name: "switchLanguage: the first read fails", start: () => switchLanguage("pl"),
    steps: [{ expect: "read", fail: offline() }] },
  { name: "switchLanguage: the save is refused", start: () => switchLanguage("pl"),
    steps: [{ expect: "read", answer: served("pl", "A") }, { expect: "save", fail: refused() }] },
  { name: "switchLanguage: a held copy that did not change (304), then the save", before: held, start: () => switchLanguage("pl"),
    steps: [{ expect: "read-unless", answer: null }, { expect: "save", answer: session("pl", "A") }] },
  { name: "switchLanguage: a held copy that changed, then the save", before: held, start: () => switchLanguage("pl"),
    steps: [{ expect: "read-unless", answer: served("pl", "B") }, { expect: "save", answer: session("pl", "B") }] },
  { name: "switchLanguage: the version check of a held copy fails", before: held, start: () => switchLanguage("pl"),
    steps: [{ expect: "read-unless", fail: offline() }] },
  { name: "switchLanguage: 409, the second read, the second save", start: () => switchLanguage("pl"),
    steps: [{ expect: "read", answer: served("pl", "A") }, { expect: "save", fail: changed() },
      { expect: "read", answer: served("pl", "B") }, { expect: "save", answer: session("pl", "B") }] },
  { name: "switchLanguage: 409 twice", start: () => switchLanguage("pl"),
    steps: [{ expect: "read", answer: served("pl", "A") }, { expect: "save", fail: changed() },
      { expect: "read", answer: served("pl", "B") }, { expect: "save", fail: changed() }] },
  { name: "switchLanguage: 409, then the second read fails", start: () => switchLanguage("pl"),
    steps: [{ expect: "read", answer: served("pl", "A") }, { expect: "save", fail: changed() }, { expect: "read", fail: offline() }] },
  { name: "switchLanguage: the save answers with another language, whose catalog is then read", start: () => switchLanguage("de"),
    steps: [{ expect: "read", answer: served("de", "A") }, { expect: "save", answer: session("pl", "A") }, { expect: "read", answer: served("pl", "A") }] },
  { name: "switchLanguage: the save answers with another language, whose read fails", start: () => switchLanguage("de"),
    steps: [{ expect: "read", answer: served("de", "A") }, { expect: "save", answer: session("pl", "A") }, { expect: "read", fail: offline() }] },
];
/** More microtasks than the store needs between an answer and its next request or its end; the
 *  first test measures that number and fails when this bound does not cover it. */
const TICKS = 16;
const POINTS = PATHS.flatMap((path) => path.steps.flatMap((_step, at) =>
  Array.from({ length: TICKS + 1 }, (_unused, ticks) => ({ path, at, ticks }))));
const tick = async (count: number) => { for (let i = 0; i < count; i += 1) await Promise.resolve(); };

describe("the end of a session at each microtask of the catalog store", () => {
  it("each path runs to its end with exactly the scripted requests, and no step needs more microtasks than the walk covers", async () => {
    let most = 0;
    for (const path of PATHS) {
      await path.before?.();
      let ended = false;
      const run = path.start().then(() => { ended = true; });
      await flush();
      for (const [at, step] of path.steps.entries()) {
        act(step);
        let ticks = 0;
        while (calls.length === at + 1 && !ended && ticks < 100) { await Promise.resolve(); ticks += 1; }
        most = Math.max(most, ticks);
        await flush();
      }
      await run;
      expect(calls.map((c) => c.name), path.name).toEqual(path.steps.map((s) => s.expect));
      endSession(); calls.length = 0;
    }
    expect(most).toBeGreaterThan(0);
    expect(most).toBeLessThanOrEqual(TICKS);
  });

  it.each(POINTS)("$path.name — ended $ticks microtasks after the answer of step $at", async ({ path, at, ticks }) => {
    await path.before?.();
    const run = path.start();
    await flush();
    for (const step of path.steps.slice(0, at)) await settle(step);

    act(path.steps[at]);
    await tick(ticks);
    const sent = calls.length;
    endSession();
    await flush();

    expect(calls, "no request after the end").toHaveLength(sent);
    if (sent > at + 1) act(path.steps[at + 1]);
    await run;
    await flush();
    expect(calls, "no request after the operation returned").toHaveLength(sent);
    expect(getCatalog().version, "no whole catalog is shown").toBeNull();
    expect(getLanguageSwitch()).toEqual({ pending: null, failed: null });

    for (const version of ["A", "B"]) {
      for (const tag of ["pl", "de"]) {
        calls.length = 0;
        const again = ensureCatalog(session(tag, version));
        await flush();
        expect(calls.map((c) => c.name), `no copy of ${tag} at ${version} is left`).toEqual(["read"]);
        calls[0].fail(offline());
        await again;
      }
    }
  });
});

describe("two shows in one session: the one that started last wins", () => {
  const switchedToGerman = async () => {
    const change = switchLanguage("de");
    await settle({ expect: "read", answer: served("de", "A") });
    await settle({ expect: "save", answer: session("de", "A") });
    expect(await change).toBe(true);
  };

  it.each([
    ["answers", (read: Call) => read.answer(served("pl", "A"))],
    ["fails", (read: Call) => read.fail(offline())],
  ])("a read that started before a language change and %s after it changes nothing", async (_name, end) => {
    const early = ensureCatalog(session("pl", "A"));
    const read = calls[0];
    await switchedToGerman();
    end(read);
    await early;
    expect(getCatalog()).toMatchObject({ locale: "de", version: "A" });
  });

  it.each([
    ["first", [0, 1]],
    ["last", [1, 0]],
  ])("of two reads, the one that started last is shown when the older one answers %s", async (_name, order) => {
    const older = ensureCatalog(session("pl", "A"));
    const newer = ensureCatalog(session("de", "A"));
    const answers = [served("pl", "A"), served("de", "A")];
    const reads = [...calls];
    for (const i of order) { reads[i].answer(answers[i]); await flush(); }
    await Promise.all([older, newer]);
    expect(getCatalog().locale).toBe("de");
  });

  it("a newer read that fails after an older one answered shows the bundled English", async () => {
    const older = ensureCatalog(session("pl", "A"));
    const newer = ensureCatalog(session("de", "A"));
    const [first, second] = [...calls];
    first.answer(served("pl", "A"));
    await flush();
    second.fail(offline());
    await Promise.all([older, newer]);
    expect(getCatalog()).toMatchObject({ locale: "en", version: null });
  });
});

describe("the source of the catalog store", () => {
  const file = join(__dirname, "catalogStore.ts");
  const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true);
  const DOORS = new Set(["live", "liveOrNothing"]);
  const REQUESTS = ["fetchCatalog", "fetchCatalogUnless", "saveLocale"];
  const line = (node: ts.Node) => `${source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1}: ${node.parent.getText(source).slice(0, 60)}`;

  type Fn = ts.FunctionDeclaration | ts.ArrowFunction | ts.FunctionExpression;
  const isFn = (node: ts.Node): node is Fn => ts.isFunctionDeclaration(node) || ts.isArrowFunction(node) || ts.isFunctionExpression(node);
  const isAsync = (fn: Fn) => !!fn.modifiers?.some((m) => m.kind === ts.SyntaxKind.AsyncKeyword);
  const isDoorCall = (node: ts.Node): node is ts.CallExpression =>
    ts.isCallExpression(node) && ts.isIdentifier(node.expression) && DOORS.has(node.expression.text);

  /** The functions of the module's top level, by name; the teardown handler is "(teardown)". */
  const topLevel = new Map<string, Fn>();
  const state: string[] = [];
  for (const statement of source.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name) topLevel.set(statement.name.text, statement);
    if (ts.isVariableStatement(statement)) {
      for (const d of statement.declarationList.declarations) {
        if (!ts.isIdentifier(d.name)) continue;
        if (statement.declarationList.flags & ts.NodeFlags.Let) state.push(d.name.text);
        else if (d.initializer && isFn(d.initializer)) topLevel.set(d.name.text, d.initializer);
      }
    }
    if (ts.isExpressionStatement(statement) && ts.isCallExpression(statement.expression)
      && statement.expression.expression.getText(source) === "onEndSession" && isFn(statement.expression.arguments[0])) {
      topLevel.set("(teardown)", statement.expression.arguments[0]);
    }
  }

  const isReference = (node: ts.Identifier): boolean => {
    const p = node.parent;
    if ((ts.isVariableDeclaration(p) || ts.isFunctionDeclaration(p) || ts.isParameter(p) || ts.isImportSpecifier(p)
      || ts.isPropertyAssignment(p) || ts.isPropertySignature(p) || ts.isInterfaceDeclaration(p)) && p.name === node) return false;
    if (ts.isPropertyAccessExpression(p) && p.name === node) return false;
    return !ts.isTypeReferenceNode(p);
  };
  const references = (root: ts.Node, names: ReadonlySet<string>): ts.Identifier[] => {
    const found: ts.Identifier[] = [];
    const visit = (node: ts.Node): void => {
      if (ts.isIdentifier(node) && names.has(node.text) && isReference(node)) found.push(node);
      ts.forEachChild(node, visit);
    };
    visit(root);
    return found;
  };

  /** The session state, the requests, and each synchronous top-level function that reaches one. */
  const guarded = new Set([...state, ...REQUESTS]);
  for (let grew = true; grew;) {
    grew = false;
    for (const [name, fn] of topLevel) {
      if (guarded.has(name) || DOORS.has(name) || name === "(teardown)" || isAsync(fn)) continue;
      if (references(fn, guarded).length) { guarded.add(name); grew = true; }
    }
  }

  const synchronousTopLevel = new Set([...topLevel.values()].filter((fn) => !isAsync(fn)));
  const firstStatementOfExportedOperation = (node: ts.Identifier): boolean => {
    const declaration = node.parent;
    const fn = declaration.parent?.parent?.parent?.parent;
    return ts.isVariableDeclaration(declaration) && declaration.getText(source) === "session = current"
      && !!fn && ts.isFunctionDeclaration(fn) && !!fn.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword)
      && fn.body?.statements[0] === declaration.parent.parent;
  };
  const throughTheDoor = (node: ts.Identifier): boolean => {
    if (isDoorCall(node.parent) && node.parent.arguments.includes(node)) return true;
    let nearest: Fn | undefined;
    for (let n: ts.Node | undefined = node.parent; n; n = n.parent) {
      if (!isFn(n)) continue;
      nearest ??= n;
      if (isDoorCall(n.parent) && n.parent.arguments.includes(n as ts.Expression)) return true;
    }
    return nearest === undefined || synchronousTopLevel.has(nearest);
  };

  it("guards the shown catalog, the language change, the session object and the requests", () => {
    expect([...guarded]).toEqual(expect.arrayContaining(
      ["current", "held", "languageSwitch", "shownAt", "lastShow", "hold", "holdUnlessNewer", "note", "shows", "newShow", ...REQUESTS]));
  });

  it("an operation reaches a guarded name only inside `live(…)`: no effect comes after an await with no session check in the same call", () => {
    const outside = references(source, guarded)
      .filter((node) => !throughTheDoor(node) && !(node.text === "current" && firstStatementOfExportedOperation(node)));
    expect(outside.map(line)).toEqual([]);
  });

  it("`current` is read only by the door, the teardown and the first statement of an exported operation", () => {
    const readers = references(source, new Set(["current"])).map((node) => {
      for (let n: ts.Node | undefined = node.parent; n; n = n.parent) {
        const name = [...topLevel].find(([, fn]) => fn === n)?.[0];
        if (name) return firstStatementOfExportedOperation(node) ? `${name}: first statement` : name;
      }
      return "(module)";
    });
    expect(readers.sort()).toEqual(["(teardown)", "ensureCatalog: first statement", "live", "switchLanguage: first statement"]);
  });

  it("nothing inside `live(…)` waits: no await, no async function, no promise chain", () => {
    const waits: string[] = [];
    const inDoor = (node: ts.Node, within: boolean): void => {
      if (within) {
        if (ts.isAwaitExpression(node) || (isFn(node) && isAsync(node))) waits.push(line(node));
        if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
          && ["then", "catch", "finally"].includes(node.expression.name.text)) waits.push(line(node));
      }
      ts.forEachChild(node, (child) => inDoor(child, within || (isDoorCall(node) && node.arguments.includes(child as ts.Expression))));
    };
    inDoor(source, false);
    expect(waits).toEqual([]);
  });

  it("the module exports no way to put a catalog in the store but the two operations of a session", () => {
    const exported = (node: ts.Statement) => ts.canHaveModifiers(node) && !!ts.getModifiers(node)?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
    const names = source.statements.filter(exported).flatMap((s) => (ts.isVariableStatement(s)
      ? s.declarationList.declarations.map((d) => d.name.getText(source))
      : ts.isFunctionDeclaration(s) && s.name ? [s.name.text] : []));
    expect(names.sort()).toEqual([
      "BUNDLED_ENGLISH", "CATALOG_DEADLINE_MS", "ensureCatalog", "forgetFailedSwitch", "getCatalog", "getLanguageSwitch",
      "subscribeCatalog", "switchLanguage", "withDeadline",
    ]);
  });

  it("the module keeps no map or set of its own but the listeners: a copy lives in the session object only", () => {
    const kept = source.statements.filter(ts.isVariableStatement).flatMap((s) => [...s.declarationList.declarations])
      .filter((d) => d.initializer && ts.isNewExpression(d.initializer)).map((d) => d.name.getText(source));
    expect(kept).toEqual(["listeners"]);
  });
});
