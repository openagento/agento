import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { ELEMENTS } from "./src/agento-ui.js";
import { CATALOGUE } from "./src/catalogue";
import { dark, light, TONES } from "./scripts/tokens";
import { blocks, main, SKILL, uncatalogued } from "./scripts/gen-skill";
import { diffDirs, KIT_VERSION, kitCss, SDK } from "./scripts/build-kit";

const css = readFileSync(join(__dirname, "src/components.css"), "utf8");

/** A fresh temp dir, removed after `body` — a test writes nothing into the repo. */
function inTemp<T>(body: (dir: string) => T): T {
  const dir = mkdtempSync(join(tmpdir(), "miniapp-kit-"));
  try { return body(dir); } finally { rmSync(dir, { recursive: true, force: true }); }
}

// WCAG 2.x relative luminance and contrast ratio, of a `#rgb`, `#rrggbb` or `rgba()` literal.
const channels = (c: string) => (c.startsWith("#")
  ? (c.length === 4 ? [...c.slice(1)].map((h) => h + h) : [1, 3, 5].map((i) => c.slice(i, i + 2))).map((h) => parseInt(h, 16))
  : c.match(/[\d.]+/g)!.slice(0, 3).map(Number));
const lum = (c: string) => {
  const [r, g, b] = channels(c).map((v) => v / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a: string, b: string) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};

describe("tokens", () => {
  const pairs: [string, string][] = [
    ["text", "background"], ["text", "surface"], ["surfaceText", "surface"], ["textMuted", "background"], ["textMuted", "surface"],
    ["link", "background"], ["primaryText", "primary"], ["primaryLightText", "primaryLight"], ["primaryLightText", "background"],
    ["error", "background"], ["error", "surface"],
    ["code", "codeBackground"], ["text", "preBackground"], ["link", "preBackground"],
    ...Object.keys(TONES).flatMap((t): [string, string][] => [[t, "background"], [`${t}LightText`, `${t}Light`]]),
  ];
  it.each([["light", light], ["dark", dark]] as const)("%s mode meets WCAG AA for text", (_, map) => {
    for (const [fg, bg] of pairs) expect(ratio(map[fg], map[bg]), `${fg} on ${bg}`).toBeGreaterThanOrEqual(4.5);
  });
});

describe("catalogue", () => {
  it("documents exactly the elements the kit defines", () => {
    expect(Object.keys(CATALOGUE).sort()).toEqual(Object.keys(ELEMENTS).sort());
  });

  it("every component class has a @catalogue block", () => {
    expect(uncatalogued(css)).toEqual([]);
    expect(blocks(css).every((b) => b.text.length && b.examples.length)).toBe(true);
  });

  it("--check refuses a stale or missing skill and accepts the committed one", () => inTemp((dir) => {
    const copy = join(dir, "SKILL.md");
    expect(() => main(["--check"], copy)).toThrow(/out of date/);
    writeFileSync(copy, readFileSync(SKILL, "utf8") + "stale");
    expect(() => main(["--check"], copy)).toThrow(/out of date/);
    expect(() => main(["--check"])).not.toThrow();
  }));
});

describe("example miniapps", () => {
  const EXAMPLES = join(__dirname, "../../miniapps/examples");
  it("use only classes the kit defines", () => {
    const defined = new Set([...css.matchAll(/\.(ag-[\w-]+)/g)].map((m) => m[1]));
    for (const ex of readdirSync(EXAMPLES)) {
      const html = readFileSync(join(EXAMPLES, ex, "index.html"), "utf8");
      const used = [...html.matchAll(/class="([^"]+)"/g)].flatMap((m) => m[1].split(/\s+/));
      expect(used.filter((c) => c.startsWith("ag-") && !defined.has(c)), ex).toEqual([]);
    }
  });
});

describe("released kit", () => {
  const released = join(__dirname, "released");
  // Built here from the sources, not read from dist/: a stale local build must not decide this.
  it("the current version is released and its bytes did not change", () => inTemp((dir) => {
    const frozen = join(released, KIT_VERSION);
    expect(existsSync(frozen)).toBe(true);
    const inter = join(__dirname, "../../node_modules/@fontsource/inter");
    writeFileSync(join(dir, "agento-ui.css"), kitCss());
    copyFileSync(join(__dirname, "src/agento-ui.js"), join(dir, "agento-ui.js"));
    copyFileSync(SDK, join(dir, "agento-bridge.js"));
    mkdirSync(join(dir, "fonts"));
    for (const f of readdirSync(join(frozen, "fonts"))) {
      copyFileSync(f === "LICENSE" ? join(inter, f) : join(inter, "files", f), join(dir, "fonts", f));
    }
    expect(diffDirs(frozen, dir)).toEqual([]);
  }));

  it("every released version keeps its fonts and bridge", () => {
    for (const v of readdirSync(released)) {
      for (const f of ["agento-ui.css", "agento-ui.js", "agento-bridge.js", "fonts/LICENSE"]) {
        expect(existsSync(join(released, v, f)), `${v}/${f}`).toBe(true);
      }
    }
  });
});

describe("custom elements", () => {
  it("ag-table sorts by a data-sort header and shows the empty text", async () => {
    document.body.innerHTML = `<ag-table data-empty="None"><table><thead><tr><th data-sort>N</th></tr></thead>
      <tbody><tr><td>b</td></tr><tr><td>a</td></tr><tr><td>10</td></tr></tbody></table></ag-table>
      <ag-table data-empty="<img src=x onerror=alert(1)>"><table><thead><tr><th>N</th></tr></thead><tbody></tbody></table></ag-table>`;
    const [t, empty] = document.querySelectorAll("ag-table");
    t.querySelector("th button")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect([...t.querySelectorAll("td")].map((td) => td.textContent)).toEqual(["10", "a", "b"]);
    expect(t.querySelector("th")!.getAttribute("aria-sort")).toBe("ascending");
    expect(empty.querySelector("td")!.textContent).toBe("<img src=x onerror=alert(1)>");
    expect(empty.querySelector("img")).toBeNull();
  });

  it("ag-table sorts the clicked column when some headers are not sortable", () => {
    document.body.innerHTML = `<ag-table><table><thead><tr><th>Name</th><th data-sort>Count</th></tr></thead>
      <tbody><tr><td>a</td><td>3</td></tr><tr><td>b</td><td>1</td></tr></tbody></table></ag-table>`;
    document.querySelector("th button")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect([...document.querySelectorAll("tbody tr td:last-child")].map((td) => td.textContent)).toEqual(["1", "3"]);
  });

  it("ag-json pretty-prints as text", () => {
    document.body.innerHTML = `<ag-json>{"a":"&lt;b&gt;x&lt;/b&gt;"}</ag-json>`;
    expect(document.querySelector("pre")!.textContent).toBe('{\n  "a": "<b>x</b>"\n}');
    expect(document.querySelector("b")).toBeNull();
  });
});
