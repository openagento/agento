// Import boundaries (PRD E8 §7, plan P4) and the bundle budget (§12). The import graph comes
// from the TypeScript pre-processor, each specifier is matched as an exact package name or a
// resolved path, and the bundle is checked on Rollup's own module list.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { dirname, join, relative, resolve, sep } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const ROOT = __dirname;
const MODULES = resolve(ROOT, "../src/agento/modules");
const BUILT = resolve(ROOT, "../src/agento/framework/web/panel");

const walk = (dir: string): string[] => readdirSync(dir).flatMap((n) => {
  const p = join(dir, n);
  if (["node_modules", "generated", "dist", "released"].includes(n)) return [];
  return statSync(p).isDirectory() ? walk(p) : /\.(tsx?|js)$/.test(n) && !/\.(test|stories)\./.test(n) ? [p] : [];
});
const importsOf = (file: string) =>
  ts.preProcessFile(readFileSync(file, "utf8"), true, true).importedFiles.map((f) => f.fileName);

/** The package a bare specifier names: `@a/b/c` → `@a/b`, `react/jsx-runtime` → `react`. */
export const packageOf = (spec: string) => spec.split("/").slice(0, spec.startsWith("@") ? 2 : 1).join("/");
const inside = (path: string, dir: string) => path === dir || path.startsWith(dir + sep);

export interface Rule { root: string; allow?: string[]; deny?: string[]; denyPaths?: string[] }

/** Every import of `file` that `rule` refuses. A relative import must stay inside `rule.root`. */
export function violations(file: string, imports: string[], rule: Rule): string[] {
  return imports.filter((spec) => {
    if (spec.startsWith(".")) {
      const target = resolve(dirname(file), spec);
      return !inside(target, rule.root) || (rule.denyPaths ?? []).some((d) => inside(target, d));
    }
    const pkg = packageOf(spec);
    if (rule.allow) return !rule.allow.includes(pkg);
    return (rule.deny ?? []).some((d) => (d.endsWith("/*") ? pkg.startsWith(d.slice(0, -1)) : pkg === d));
  });
}

const MODULE_PANEL: Omit<Rule, "root"> = { allow: ["react", "react-router", "@agento/ui", "@agento/api"] };
const UI: Rule = { root: join(ROOT, "packages/ui"), deny: ["@agento/api", "@agento/miniapp-bridge"] };
const KIT: Rule = { root: join(ROOT, "packages/miniapp-kit"), deny: ["react", "react-dom", "@mantine/*", "@agento/ui", "@agento/api"] };

const check = (files: string[], rule: (f: string) => Rule) =>
  files.flatMap((f) => violations(f, importsOf(f), rule(f)).map((i) => `${relative(ROOT, f)} → ${i}`));

describe("import boundaries", () => {
  const panels = readdirSync(MODULES).map((m) => join(MODULES, m, "panel"))
    .filter((p) => { try { return statSync(p).isDirectory(); } catch { return false; } });

  it("a module panel imports only react, react-router, @agento/ui, @agento/api and its own files", () => {
    expect(panels.length).toBeGreaterThan(0);
    expect(check(panels.flatMap(walk), (f) => ({ ...MODULE_PANEL, root: panels.find((p) => inside(f, p))! }))).toEqual([]);
  });

  it("@agento/ui imports no API, bridge, panel or module code", () => {
    // acceptance/ is story support (not exported): it reads the example miniapp on purpose.
    expect(check(walk(UI.root).filter((f) => !f.includes(`${sep}acceptance${sep}`)), () => UI)).toEqual([]);
  });

  it("the miniapp kit imports no React, Mantine or panel package", () => {
    expect(check(walk(KIT.root).filter((f) => !f.includes(`${sep}scripts${sep}`)), () => KIT)).toEqual([]);
  });

  it("nothing imports the lookbook (RULES.md UI-5)", () => {
    const lookbook = join(ROOT, "lookbook");
    const files = [...walk(join(ROOT, "panel")), ...walk(join(ROOT, "packages")), ...panels.flatMap(walk)];
    expect(files.flatMap((f) => importsOf(f).filter((s) => s.startsWith(".") && inside(resolve(dirname(f), s), lookbook))
      .map((s) => `${relative(ROOT, f)} → ${s}`))).toEqual([]);
  });

  describe("the checker refuses", () => {
    const panel = join(MODULES, "conversation/panel");
    const rule = { ...MODULE_PANEL, root: panel };
    const f = join(panel, "Page.tsx");
    it.each([
      ["react-dom/client"], ["@mantine/core"], ["@agento/miniapp-bridge"], ["../src/service"],
      ["../../conversation-other/panel/x"], ["../../miniapps/sdk/agento-sdk.js"],
    ])("%s from a module panel", (spec) => expect(violations(f, [spec], rule)).toEqual([spec]));
    it("a sibling directory with the same prefix", () =>
      expect(violations(f, ["../panel-other/x"], rule)).toEqual(["../panel-other/x"]));
    it("@agento/api from @agento/ui", () =>
      expect(violations(join(UI.root, "src/x.tsx"), ["@agento/api"], UI)).toEqual(["@agento/api"]));
    it("react and Mantine from the kit", () =>
      expect(violations(join(KIT.root, "src/x.ts"), ["react", "@mantine/hooks"], KIT)).toEqual(["react", "@mantine/hooks"]));
    it("but allows its own files and the allowed packages", () =>
      expect(violations(f, ["./model", "react/jsx-runtime", "@agento/ui"], rule)).toEqual([]));
  });
});

/** An exact package name, or `@scope/` for every package in a scope. */
const FORBIDDEN = ["codemirror", "@codemirror/", "monaco-editor", "chart.js", "recharts", "echarts", "xterm", "@xterm/"];
export const forbidden = (pkg: string) => FORBIDDEN.some((f) => (f.endsWith("/") ? pkg.startsWith(f) : pkg === f));

describe("bundle budget", () => {
  const chunks: { file: string; entry: boolean; modules: string[] }[] =
    JSON.parse(readFileSync(join(ROOT, "panel/bundle-modules.json"), "utf8"));
  const pkgs = new Set(chunks.flatMap((c) => c.modules).flatMap((id) => {
    const m = /node_modules\/((?:@[^/]+\/)?[^/]+)/.exec(id);
    return m ? [m[1]] : [];
  }));

  it("bundles no editor, chart or terminal package", () => {
    expect(pkgs.size).toBeGreaterThan(0);
    expect([...pkgs].filter(forbidden)).toEqual([]);
  });

  it.each([["@codemirror/state"], ["@xterm/xterm"], ["codemirror"], ["monaco-editor"], ["recharts"]])(
    "the guard refuses %s", (p) => expect(forbidden(p)).toBe(true));
  it.each([["@mantine/core"], ["react"], ["xterm-like"]])("the guard allows %s", (p) => expect(forbidden(p)).toBe(false));

  it("the entry chunk stays under 120 kB gzip", () => {
    const entry = chunks.find((c) => c.entry)!;
    const size = gzipSync(readFileSync(join(BUILT, entry.file))).length;
    console.info(`panel entry ${entry.file}: ${size} B gzip`);
    expect(size).toBeLessThan(120 * 1024);
  });
});
