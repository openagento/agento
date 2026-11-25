// A text a user reads is translated when it is rendered, so a language change re-translates it.
// A text made in a callback (a promise answer, a mutation result, a catch) or given to a state
// setter is made once and stays in the language of that moment: keep the error or the key with
// its parameters, and translate at render. A function that makes a text and is given to another
// call counts too, unless that call runs it in the same render (an array method, a React hook).
// A notification is the one exemption: it is the record of one event, written in the language
// shown at that event. The scan does not follow a text, or a function that makes one, through
// a variable.
import { join, relative } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const ROOT = __dirname;
const SCREEN_SOURCE = /^(packages\/(ui|api)\/src|panel\/src|\.\.\/src\/agento\/modules\/[^/]+\/panel)\//;
const NOT_A_SCREEN = /\.(test|stories)\.|\/generated\/|\/acceptance\/|\/testing\.tsx$/;
const TEXT_MAKERS = new Set(["Translate", "Lookup", "Formatters", "useErrorText", "useExplain", "useJobStatusText", "dayTime"]);
const SAME_RENDER = /(^|\.)(map|flatMap|filter|find|some|every|forEach|reduce|sort|toSorted|useMemo|useCallback|useEffect|useLayoutEffect)$/;
const LATER = new Set(["then", "catch", "finally"]);
const RESULT_HANDLERS = new Set(["onError", "onSuccess", "onSettled", "onMutate"]);

function makerOf(checker: ts.TypeChecker, call: ts.CallExpression): string | undefined {
  let node: ts.Node | undefined = checker.getResolvedSignature(call)?.declaration;
  for (; node; node = node.parent) {
    const named = (ts.isTypeAliasDeclaration(node) || ts.isInterfaceDeclaration(node) || ts.isFunctionDeclaration(node)) && node.name?.text;
    if (named && TEXT_MAKERS.has(named)) return named;
  }
  return undefined;
}

const isFunction = (node: ts.Node) => ts.isArrowFunction(node) || ts.isFunctionExpression(node);
const calledAs = (call: ts.CallExpression) => (ts.isPropertyAccessExpression(call.expression) ? call.expression.name.text : undefined);

function whereItIsKept(checker: ts.TypeChecker, source: ts.SourceFile, text: ts.CallExpression): string | undefined {
  for (let node: ts.Node = text; node.parent; node = node.parent) {
    const parent = node.parent;
    if (ts.isCallExpression(parent) && parent.arguments.includes(node as ts.Expression)) {
      if (parent.expression.getText(source) === "notifications.show") return undefined;
      if (checker.typeToString(checker.getTypeAtLocation(parent.expression)).startsWith("Dispatch<SetStateAction<")) return "a state setter";
      if (isFunction(node) && LATER.has(calledAs(parent) ?? "")) return `.${calledAs(parent)}()`;
      if (isFunction(node) && !SAME_RENDER.test(parent.expression.getText(source))) return `a function given to ${parent.expression.getText(source)}()`;
    }
    if (isFunction(node) && ts.isPropertyAssignment(parent) && RESULT_HANDLERS.has(parent.name.getText(source))) return parent.name.getText(source);
    if (ts.isCatchClause(parent)) return "a catch block";
  }
  return undefined;
}

function textsKeptPastTheirRender(): string[] {
  const config = ts.getParsedCommandLineOfConfigFile(join(ROOT, "tsconfig.json"), {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} })!;
  const program = ts.createProgram(config.fileNames, config.options);
  const checker = program.getTypeChecker();
  const found: string[] = [];
  for (const source of program.getSourceFiles()) {
    const name = relative(ROOT, source.fileName);
    if (source.isDeclarationFile || !SCREEN_SOURCE.test(name) || NOT_A_SCREEN.test(name)) continue;
    const visit = (node: ts.Node): void => {
      if (ts.isCallExpression(node) && makerOf(checker, node)) {
        const kept = whereItIsKept(checker, source, node);
        if (kept) found.push(`${name}:${source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1} ${node.expression.getText(source)}(…) in ${kept}`);
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  return found;
}

describe("a translated text", () => {
  it("is made at render, never kept in state or made in a late callback", () => {
    expect(textsKeptPastTheirRender()).toEqual([]);
  }, 60_000);
});
