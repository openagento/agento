// A number a user reads goes through a formatter (`useFormat`, a `{param}` of `t`), so it has the
// digits and the grouping of the panel's language. An id is not a number to format: write it
// with `String(id)`. This scan fails on a bare number in JSX text; it reads types, not names.
import { join, relative } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const ROOT = __dirname;
const SCREEN_SOURCE = /^(packages\/ui\/src|panel\/src|\.\.\/src\/agento\/modules\/[^/]+\/panel)\//;
const PANEL_SOURCE = /^(packages\/(ui|api)\/src|panel\/src|\.\.\/src\/agento\/modules\/[^/]+\/panel)\//;
const NOT_A_SCREEN = /\.(test|stories)\.|\/generated\/|\/acceptance\//;

const isNumber = (type: ts.Type) => (type.flags & ts.TypeFlags.NumberLike) !== 0;
const isAbsent = (type: ts.Type) => (type.flags & (ts.TypeFlags.Null | ts.TypeFlags.Undefined)) !== 0;
const onlyANumber = (type: ts.Type): boolean => (type.isUnion()
  ? type.types.some(isNumber) && type.types.every((member) => isNumber(member) || isAbsent(member))
  : isNumber(type));

const INTL_MODULE = "packages/ui/src/intl.ts";
const LOCALE_METHOD = /^(toLocale(Date|Time)?String|toLocale(Upper|Lower)Case|localeCompare)$/;
const usesIntl = (node: ts.Node) => ts.isPropertyAccessExpression(node)
  && ((ts.isIdentifier(node.expression) && node.expression.text === "Intl") || LOCALE_METHOD.test(node.name.text));

let program: ts.Program | undefined;
function panelProgram(): ts.Program {
  const config = ts.getParsedCommandLineOfConfigFile(join(ROOT, "tsconfig.json"), {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} })!;
  program ??= ts.createProgram(config.fileNames, config.options);
  return program;
}

/** `file:line text` of each node of a screen source that `wanted` accepts. */
function inScreenSources(wanted: (node: ts.Node, checker: ts.TypeChecker) => boolean, sources = SCREEN_SOURCE): string[] {
  const checker = panelProgram().getTypeChecker();
  const found: string[] = [];
  for (const source of panelProgram().getSourceFiles()) {
    const name = relative(ROOT, source.fileName);
    if (source.isDeclarationFile || !sources.test(name) || NOT_A_SCREEN.test(name)) continue;
    const visit = (node: ts.Node): void => {
      if (wanted(node, checker)) found.push(`${name}:${source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1} ${node.getText(source)}`);
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  return found;
}

const bareNumberInJsxText = (node: ts.Node, checker: ts.TypeChecker) =>
  ts.isJsxExpression(node) && !!node.expression && (ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent))
  && onlyANumber(checker.getTypeAtLocation(node.expression));

describe("numbers on a screen", () => {
  it("no JSX text is a bare number: each one goes through a formatter", { timeout: 60_000 }, () => {
    expect(inScreenSources(bareNumberInJsxText)).toEqual([]);
  });

  // `Intl` with a tag it has no data for, and a `toLocale…` call with no tag, write in the default
  // language of the browser. `intl.ts` gives each formatter the tag and then English.
  it("only `intl.ts` calls `Intl` or a `toLocale…` method", { timeout: 60_000 }, () => {
    const found = inScreenSources(usesIntl, PANEL_SOURCE);
    expect(found.filter((use) => use.startsWith(INTL_MODULE)).length).toBeGreaterThan(0);
    expect(found.filter((use) => !use.startsWith(INTL_MODULE))).toEqual([]);
  });
});
