import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { describe, expect, it } from "vitest";

type Message = string | Record<string, string>;
type Namespace = Record<string, Message>;

const here = (p: string) => fileURLToPath(new URL(p, import.meta.url));
const SOURCE = "en";
const FRAMEWORK = here("../src/agento/framework/i18n");
const MODULES = here("../src/agento/modules");
const dirs = (root: string) => readdirSync(root).filter((name) => statSync(join(root, name)).isDirectory());

const CATALOG_DIR_OF_NAMESPACE: Record<string, string> = Object.fromEntries([
  ...dirs(FRAMEWORK).map((name) => [name, join(FRAMEWORK, name)]),
  ...dirs(MODULES).map((name) => [name, join(MODULES, name, "i18n")]),
].filter(([, dir]) => existsSync(dir) && tagsOf(dir).length > 0));
function tagsOf(dir: string): string[] {
  return readdirSync(dir).filter((f) => /^[a-z]{2}\.json$/.test(f)).map((f) => f.slice(0, 2));
}
function read(namespace: string, tag: string): Namespace {
  const file = join(CATALOG_DIR_OF_NAMESPACE[namespace] ?? "", `${tag}.json`);
  return existsSync(file) ? JSON.parse(readFileSync(file, "utf8")) : {};
}
const CATALOGS = Object.keys(CATALOG_DIR_OF_NAMESPACE).flatMap((namespace) =>
  tagsOf(CATALOG_DIR_OF_NAMESPACE[namespace]).map((tag) => ({ namespace, tag, messages: read(namespace, tag) })));
const TRANSLATIONS = CATALOGS.filter((c) => c.tag !== SOURCE);

const texts = (m: Message) => (typeof m === "string" ? [m] : Object.values(m));
const holes = (m: Message) => [...new Set(texts(m).flatMap((text) => [...text.matchAll(/\{(\w+)\}/g)].map((hole) => hole[1])))].sort();

const SAME_WORD_IN_BOTH_LANGUAGES = new Set(["Agent", "agent", "Model", "Status", "Prompt", "Harness", "Ingress", "ID", "ok", "Python {version}"]);
const GLOSSARY: Record<string, Record<string, string>> = JSON.parse(readFileSync(join(FRAMEWORK, "glossary.json"), "utf8"));
const HOLE_OR_IDENTIFIER = /\{\w+\}|\S*\w[:.]\w\S*/g;
const lacksTheTerm = (english: Message, translated: Message, term: string, stem: string) =>
  new RegExp(`\\b${term}`, "i").test(texts(english).join(" ").replace(HOLE_OR_IDENTIFIER, "")) && !texts(translated).every((text) => new RegExp(stem, "iu").test(text));

describe("the shipped catalogs", () => {
  it("hold the two framework namespaces in English and Polish", () => {
    expect(CATALOGS.map((c) => `${c.namespace}/${c.tag}`)).toEqual(expect.arrayContaining(["ui/en", "ui/pl", "panel/en", "panel/pl"]));
  });

  it.each(TRANSLATIONS)("$namespace/$tag holds every English key, with the same parameters", ({ namespace, messages }) => {
    const english = read(namespace, SOURCE);
    expect(Object.keys(english).filter((key) => !(key in messages))).toEqual([]);
    expect(Object.keys(english).filter((key) => key in messages && String(holes(messages[key])) !== String(holes(english[key]))))
      .toEqual([]);
  });

  it.each(TRANSLATIONS)("$namespace/$tag makes a plural of every English plural", ({ namespace, messages }) => {
    const english = read(namespace, SOURCE);
    expect(Object.keys(english).filter((key) => key in messages && typeof english[key] !== typeof messages[key])).toEqual([]);
  });

  it.each(TRANSLATIONS)("$namespace/$tag translates every text that has letters", ({ namespace, messages }) => {
    const english = read(namespace, SOURCE);
    const copied = Object.keys(english).filter((key) => key in messages
      && JSON.stringify(messages[key]) === JSON.stringify(english[key])
      && texts(english[key]).some((text) => /\p{L}{2}/u.test(text) && !SAME_WORD_IN_BOTH_LANGUAGES.has(text)));
    expect(copied).toEqual([]);
  });

  it.each(TRANSLATIONS.filter((c) => c.tag in GLOSSARY))("$namespace/$tag uses the glossary's word for each term", ({ namespace, tag, messages }) => {
    const english = read(namespace, SOURCE);
    const wrong = Object.entries(GLOSSARY[tag]).flatMap(([term, stem]) => Object.keys(english)
      .filter((key) => key in messages && lacksTheTerm(english[key], messages[key], term, stem)).map((key) => `${key}: ${term}`));
    expect(wrong).toEqual([]);
  });

  it("the glossary check refuses a second word for a term", () => {
    expect(lacksTheTerm("Lethal trifecta", "Zabójcza trójka", "lethal trifecta", GLOSSARY.pl["lethal trifecta"])).toBe(true);
    expect(lacksTheTerm("Lethal trifecta", "Śmiertelnej trójki", "lethal trifecta", GLOSSARY.pl["lethal trifecta"])).toBe(false);
    expect(lacksTheTerm("Run credential:list for {credential}.", "Uruchom credential:list dla {credential}.", "credential", GLOSSARY.pl.credential)).toBe(false);
  });

  it.each(CATALOGS)("$namespace/$tag gives each plural exactly the forms its language has", ({ tag, messages }) => {
    const forms = [...new Intl.PluralRules(tag).resolvedOptions().pluralCategories].sort();
    const wrong = Object.entries(messages)
      .filter(([, message]) => typeof message !== "string" && String(Object.keys(message).sort()) !== String(forms))
      .map(([key]) => key);
    expect(wrong).toEqual([]);
  });

  it("the discard warning of a role agrees with one change and with many", () => {
    expect(read("panel", SOURCE)["roles.discard.text"]).toMatchObject({
      one: expect.stringContaining("{count} change in this place is not saved"),
      other: expect.stringContaining("{count} changes in this place are not saved"),
    });
  });

  it("holds every sign-in key the server sends before a session exists", () => {
    const login = Object.keys(read("panel", SOURCE)).filter((key) => key.startsWith("login."));
    expect(login.sort()).toEqual([
      "login.document_title", "login.error.AUTH_INVALID_CREDENTIALS", "login.error.AUTH_THROTTLED",
      "login.error.RATE_LIMITED", "login.error.generic", "login.password", "login.submit", "login.title", "login.username",
    ]);
  });
});

const SOURCE_ROOTS = [here("packages"), here("panel/src"), ...dirs(MODULES).map((name) => join(MODULES, name, "panel"))];
const isSource = (file: string) => /\.tsx?$/.test(file) && !/\.(test|stories)\.tsx?$/.test(file) && !file.endsWith(".d.ts");
function sourceFiles(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    if (entry.name === "node_modules" || entry.name === "dist" || entry.name === "generated") return [];
    const path = join(dir, entry.name);
    return entry.isDirectory() ? sourceFiles(path) : isSource(path) ? [path] : [];
  });
}

interface Use { file: string; namespace: string; key: string; orPanel?: true }
interface Family { namespace?: string; prefix: string }
const literal = (node: ts.Node | undefined) => (node && ts.isStringLiteralLike(node) ? node.text : undefined);
const calls = (node: ts.Node, name: string): node is ts.CallExpression =>
  ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === name;

const templateHead = (node: ts.Node | undefined) =>
  node && ts.isTemplateExpression(node) ? node.head.text : undefined;

function literalKeysAndDynamicCalls(file: string): { uses: Use[]; dynamic: string[]; families: Family[] } {
  const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true);
  const name = relative(here("."), file);
  const translators = new Map<string, string>();
  const errorTexts = new Map<string, string>();
  const lookups = new Set<string>();
  const passedTranslators = new Set<string>();
  const bind = (node: ts.Node): void => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      if (calls(node.initializer, "useT")) translators.set(node.name.text, literal(node.initializer.arguments[0]) ?? "");
      if (calls(node.initializer, "useErrorText")) errorTexts.set(node.name.text, literal(node.initializer.arguments[0]) ?? "panel");
      if (calls(node.initializer, "useLookup")) lookups.add(node.name.text);
    }
    if (ts.isParameter(node) && ts.isIdentifier(node.name) && node.type?.getText(source) === "Translate") passedTranslators.add(node.name.text);
    ts.forEachChild(node, bind);
  };
  bind(source);
  const namespaces = [...new Set(translators.values())];
  passedTranslators.forEach((t) => { if (!translators.has(t) && namespaces.length === 1) translators.set(t, namespaces[0]); });

  const uses: Use[] = [];
  const dynamic: string[] = [];
  const families: Family[] = [];
  const at = (node: ts.Node) => `${name}:${source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1}`;
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
      const callee = node.expression.text;
      const [first, second, third] = node.arguments;
      if (translators.has(callee)) {
        const key = literal(first);
        if (key === undefined) dynamic.push(at(node));
        else uses.push({ file: at(node), namespace: translators.get(callee)!, key });
      } else if (errorTexts.has(callee) && literal(second) !== undefined) {
        uses.push({ file: at(node), namespace: errorTexts.get(callee)!, key: literal(second)!, orPanel: true });
      } else if (lookups.has(callee) && literal(first) !== undefined && literal(second) !== undefined) {
        uses.push({ file: at(node), namespace: literal(first)!, key: literal(second)! });
      } else if (lookups.has(callee) && (templateHead(second) || literal(second))) {
        families.push({ namespace: literal(first), prefix: templateHead(second) ?? literal(second)! });
      } else if (callee === "translate" && literal(second) !== undefined && literal(third) !== undefined) {
        uses.push({ file: at(node), namespace: literal(second)!, key: literal(third)! });
      }
    }
    if (ts.isPropertyAssignment(node) && node.name.getText(source) === "fallbackKey" && literal(node.initializer) !== undefined) {
      uses.push({ file: at(node), namespace: [...errorTexts.values()][0] ?? "panel", key: literal(node.initializer)!, orPanel: true });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return { uses, dynamic, families };
}

const MODULES_WITH_SCREENS = dirs(MODULES).filter((name) => existsSync(join(MODULES, name, "panel", "index.ts")));
const KEYS_WITH_ENGLISH_OUTSIDE_A_CATALOG = ["config.", "acl.", "module.", "nav.", "joblink", "limit."];

describe("the keys the source asks for", () => {
  const scanned = SOURCE_ROOTS.flatMap(sourceFiles).map(literalKeysAndDynamicCalls);

  it("no shipped file switches off a catalog rule", () => {
    const switchedOff = /eslint-disable(?!(-next)?-line (?!.*no-restricted-syntax)\S)/;
    expect(SOURCE_ROOTS.flatMap(sourceFiles).filter((file) => switchedOff.test(readFileSync(file, "utf8")))
      .map((file) => relative(here("."), file))).toEqual([]);
  });
  const uses = scanned.flatMap((s) => s.uses);

  it("finds the calls of the translated screens", () => {
    expect(uses.length).toBeGreaterThan(100);
    expect(new Set(uses.map((u) => u.namespace))).toEqual(new Set(["ui", "panel", ...MODULES_WITH_SCREENS]));
  });

  it("every literal key is in the English catalog of its namespace", () => {
    const held = (u: Use) => u.key in read(u.namespace, SOURCE) || (u.orPanel === true && u.key in read("panel", SOURCE));
    expect(uses.filter((u) => !held(u)).map((u) => `${u.file} ${u.namespace}:${u.key}`)).toEqual([]);
  });

  it.each(Object.keys(CATALOG_DIR_OF_NAMESPACE))("%s holds no English key that no source asks for", (namespace) => {
    const asked = new Set(uses.filter((u) => u.namespace === namespace || (u.orPanel && namespace === "panel")).map((u) => u.key));
    const ofAModule = !dirs(FRAMEWORK).includes(namespace);
    const prefixes = scanned.flatMap((s) => s.families).filter((f) => f.namespace === namespace || (f.namespace === undefined && ofAModule))
      .map((f) => f.prefix);
    expect(Object.keys(read(namespace, SOURCE)).filter((key) => !asked.has(key) && !prefixes.some((prefix) => key.startsWith(prefix))))
      .toEqual([]);
  });

  it("every `t(…)` call names its key as a literal, so this scan sees it", () => {
    expect(scanned.flatMap((s) => s.dynamic)).toEqual([]);
  });
});

function textsOfDescriptor(module: string): string[] {
  const file = join(MODULES, module, "panel", "index.ts");
  const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true);
  const keys: string[] = [];
  const property = (object: ts.ObjectLiteralExpression, name: string) => object.properties
    .find((p): p is ts.PropertyAssignment => ts.isPropertyAssignment(p) && p.name.getText(source) === name)?.initializer;
  const visit = (node: ts.Node): void => {
    if (ts.isObjectLiteralExpression(node)) {
      if (property(node, "nav")) keys.push(`nav.${literal(property(node, "path"))}`);
      if (property(node, "jobLink")) keys.push("joblink");
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return keys;
}

describe("the texts of a module descriptor", () => {
  it("finds the descriptors", () => {
    expect(MODULES_WITH_SCREENS.flatMap(textsOfDescriptor)).toEqual(expect.arrayContaining(["nav./conversations", "joblink"]));
  });

  it.each(MODULES_WITH_SCREENS.flatMap((module) => TRANSLATIONS.filter((c) => c.namespace === module)))(
    "$namespace/$tag names every menu entry and job link of the descriptor", ({ namespace, messages }) => {
      expect(textsOfDescriptor(namespace).filter((key) => !(key in messages))).toEqual([]);
    });

  it.each(TRANSLATIONS)("$namespace/$tag holds a key English lacks only where the English text is not in a catalog", ({ namespace, messages }) => {
    const english = read(namespace, SOURCE);
    expect(Object.keys(messages).filter((key) => !(key in english) && !KEYS_WITH_ENGLISH_OUTSIDE_A_CATALOG.some((prefix) => key.startsWith(prefix))))
      .toEqual([]);
  });
});
