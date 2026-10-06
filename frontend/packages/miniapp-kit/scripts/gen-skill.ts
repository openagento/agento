// Generates the miniapp authoring skill (PRD E8 §10.4) from the `@catalogue` comments in
// components.css and the custom-element CATALOGUE, so the skill never drifts from the kit.
// Output: src/agento/modules/miniapps/skills/miniapp-ui/SKILL.md. A test compares it.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { CATALOGUE } from "../src/catalogue.ts";

const KIT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO = resolve(KIT, "../../..");
export const SKILL = join(REPO, "src/agento/modules/miniapps/skills/miniapp-ui/SKILL.md");
const VERSION: string = JSON.parse(readFileSync(join(KIT, "package.json"), "utf8")).version;

export type Block = { selector: string; text: string[]; examples: string[] };

/** Every `@catalogue` block of the CSS, in file order. */
export function blocks(css: string): Block[] {
  return [...css.matchAll(/\/\* @catalogue (\.[\w-]+)\n([\s\S]*?)\*\//g)].map(([, selector, body]) => {
    const lines = body.split("\n").map((l) => l.trim()).filter(Boolean);
    return {
      selector,
      text: lines.filter((l) => !l.startsWith("<")),
      examples: lines.filter((l) => l.startsWith("<")),
    };
  });
}

/** Top-level `.ag-x` class names that have a rule but no catalogue block. */
export function uncatalogued(css: string): string[] {
  // A block documents its own selector and any `.ag-x` its text names (`.ag-row` lives in `.ag-stack`).
  const documented = new Set(blocks(css).flatMap((b) =>
    [b.selector, ...b.text.flatMap((t) => [...t.matchAll(/`(\.ag-[\w-]+)`/g)].map((m) => m[1]))]));
  const ruled = new Set([...css.matchAll(/^(?:[\w]+)?(\.ag-[a-z-]+)(?![\w-])/gm)].map((m) => m[1]));
  return [...ruled].filter((s) => !documented.has(s) && !s.includes("__") && !s.includes("--"));
}

export function render(css: string): string {
  const ui = `/_ui/${VERSION}`;
  const out = [
    // Codex refuses a SKILL.md without this frontmatter (DECISIONS.md, module skills carry frontmatter).
    "---",
    "name: miniapp-ui",
    "description: Use when you write the HTML of a miniapp page — the Agento kit classes, elements and layout rules. For the build steps, use the miniapp-build skill.",
    "---",
    "",
    "# Miniapp UI",
    "",
    `Use when you write a miniapp page: hand-written HTML over the Agento kit ${VERSION} (\`.ag-*\` classes, \`<ag-*>\` elements). Generated from frontend/packages/miniapp-kit; do not edit.`,
    "",
    "## Page skeleton",
    "",
    "Load the kit from the apps origin. Write plain HTML; set every text with `textContent`, never `innerHTML`.",
    "",
    "```html",
    "<!doctype html>",
    '<html lang="en">',
    "<head>",
    '  <meta charset="utf-8">',
    '  <meta name="viewport" content="width=device-width, initial-scale=1">',
    "  <title>My app</title>",
    `  <link rel="stylesheet" href="${ui}/agento-ui.css">`,
    `  <script type="module" src="${ui}/agento-ui.js"></script>`,
    "</head>",
    '<body class="ag-app">',
    '  <main class="ag-page ag-stack">…</main>',
    '  <script type="module">',
    `    import { createAgentoSdk } from "${ui}/agento-bridge.js";`,
    '    const sdk = createAgentoSdk({ panelOrigin: "https://<panel host>" });',
    '    // sdk.callAction("<tool listed in miniapp.json actions>", { ... }) returns a Promise.',
    "  </script>",
    "</body>",
    "</html>",
    "```",
    "",
    "Add `miniapp.json` next to `index.html`: `{\"schema\": 1, \"title\": \"…\", \"actions\": []}`. An action is a declared tool name; it works only after an operator activates the version.",
    "",
    "## Layout rules",
    "",
    "- The page must work at 320px wide with no sideways page scroll. Put wide tables in `<ag-table>`.",
    "- Under 480px, `.ag-row` wraps; do not set fixed widths.",
    "- From 768px, `.ag-page` gets larger gutters. Design for 320, check 480 and 768.",
    "- Use only the `--ag-*` variables for color. Light and dark modes come from the kit.",
    "- No inline `style`, no other stylesheet, no external script.",
    "",
    "## Classes",
    "",
  ];
  for (const b of blocks(css)) {
    out.push(`### \`${b.selector}\``, "", ...b.text, "", "```html", ...b.examples, "```", "");
  }
  out.push("## Elements", "");
  for (const [name, { description, example }] of Object.entries(CATALOGUE)) {
    out.push(`### \`<${name}>\``, "", description, "", "```html", example, "```", "");
  }
  return out.join("\n");
}

/** `--check` writes nothing and fails when the committed skill is not the generated one. */
export function main(argv: string[]): void {
  const css = readFileSync(join(KIT, "src/components.css"), "utf8");
  const missing = uncatalogued(css);
  if (missing.length) throw new Error(`components.css: no @catalogue block for ${missing.join(", ")}`);
  if (argv.includes("--check")) {
    let committed = "";
    try { committed = readFileSync(SKILL, "utf8"); } catch { /* missing = drift */ }
    if (committed !== render(css)) throw new Error(`${SKILL} is out of date: run \`npm run gen\` and commit it`);
    return;
  }
  mkdirSync(dirname(SKILL), { recursive: true });
  writeFileSync(SKILL, render(css));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main(process.argv.slice(2));
