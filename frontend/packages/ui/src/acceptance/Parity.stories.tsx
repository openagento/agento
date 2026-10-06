// The panel renders Mantine components; a miniapp writes `.ag-*` markup over agento-ui.css.
// Each pair below is the same widget in both runtimes; their computed styles must be equal, in
// light and dark, so a theme change or a Mantine upgrade that the kit does not follow fails here.
import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, waitFor } from "storybook/test";
import { Button, type ButtonVariant } from "../components/Button";
import { DataTable } from "../components/DataTable";
import { SelectField, TextField } from "../components/Form";
import { Markdown } from "../components/Markdown";
import { StatusBadge, type BadgeTone } from "../components/StatusBadge";

const BOX = ["background-color", "color", "border-top-color", "border-top-width", "border-top-left-radius",
  "height", "padding-left", "font-family", "font-size", "font-weight", "line-height"];
const TEXT = ["color", "font-family", "font-size", "font-weight", "line-height", "display"];
const CELL = ["color", "font-size", "font-weight", "line-height", "padding-top", "padding-left", "text-align"];

const SPACE = ["margin-top", "margin-bottom", "padding-top", "padding-left"];
/** [selector inside the prose pair, compared properties]: what react-markdown + GFM emits. */
const PROSE: [string, string[]][] = [
  ["h2", [...TEXT, ...SPACE]],
  ["p", [...TEXT, ...SPACE]],
  ["a", [...TEXT, "text-decoration-line"]],
  ["p > code", [...BOX, ...SPACE, "border-top-left-radius"]],
  ["ul", [...TEXT, ...SPACE, "list-style-position"]],
  ["pre", [...BOX, ...SPACE, "overflow-x"]],
  ["pre code", [...BOX, ...SPACE]],
  ["th", [...CELL, "border-bottom-color", "border-bottom-width", "padding-bottom"]],
  ["td", [...CELL, "border-bottom-color", "border-bottom-width", "padding-bottom"]],
  ["blockquote", [...BOX, ...SPACE]],
  ["hr", ["border-top-color", "border-top-width", "border-top-style", ...SPACE]],
];
const PROSE_MD = `## Summary

The import **finished**. See [the log](https://example.com/log) and run \`bin/agento replay 42\`.

- 120 rows read
- 3 rows skipped

\`\`\`json
{"ok": true}
\`\`\`

| Step | State |
|---|---|
| Read | done |
| Write | done |

> Next run at 02:00.

---

Last line.`;
const PROSE_HTML = `<h2>Summary</h2><p>The import <strong>finished</strong>. See <a href="https://example.com/log" target="_blank" rel="noopener noreferrer">the log</a> and run <code>bin/agento replay 42</code>.</p>
<ul><li>120 rows read</li><li>3 rows skipped</li></ul><pre><code class="language-json">{"ok": true}
</code></pre><table><thead><tr><th>Step</th><th>State</th></tr></thead><tbody><tr><td>Read</td><td>done</td></tr><tr><td>Write</td><td>done</td></tr></tbody></table>
<blockquote><p>Next run at 02:00.</p></blockquote><hr><p>Last line.</p>`;

const ROLES = [{ value: "user", label: "user" }, { value: "admin", label: "admin" }];
const BUTTONS: ButtonVariant[] = ["default", "primary", "subtle", "danger"];
const TONES: BadgeTone[] = ["neutral", "running", "succeeded", "failed", "blocked"];

/** [label, panel element, miniapp element, compared properties] */
type Pair = [string, Element, Element, string[]];

function pairs(root: Element): Pair[] {
  const panel = root.querySelector('[data-side="panel"]')!;
  const kit = root.querySelector('[data-side="miniapp"]')!;
  const both = (sel: string, kitSel = sel) => [panel.querySelectorAll(sel), kit.querySelectorAll(kitSel)] as const;
  const out: Pair[] = [];
  const [pb, kb] = both("[data-pair=button] button");
  BUTTONS.forEach((v, i) => out.push([`button ${v}`, pb[i], kb[i], [...BOX, "border-left-color"]]));
  const [pd, kd] = both("[data-pair=badge] > *");
  TONES.forEach((t, i) => out.push([`badge ${t}`, pd[i], kd[i], [...BOX, "text-transform", "letter-spacing"]]));
  out.push(["field label", panel.querySelector("[data-pair=field] label")!, kit.querySelector("[data-pair=field] label")!, TEXT]);
  out.push(["field input", panel.querySelector("[data-pair=field] input")!, kit.querySelector("[data-pair=field] input")!, BOX]);
  out.push(["field hint", panel.querySelector("[data-pair=field] [id$=description]")!, kit.querySelector("[data-pair=field] .ag-field__hint")!, TEXT]);
  out.push(["select input", panel.querySelector("[data-pair=select] input[role=combobox]")!, kit.querySelector("[data-pair=select] select")!,
    [...BOX, "padding-right"]]);
  out.push(["contained label", panel.querySelector("[data-pair=contained] label")!, kit.querySelector("[data-pair=contained] label")!,
    [...TEXT, "position", "padding-top", "padding-left", "z-index"]]);
  out.push(["contained input", panel.querySelector("[data-pair=contained] input")!, kit.querySelector("[data-pair=contained] input")!,
    [...BOX, "padding-top"]]);
  out.push(["table th", panel.querySelector("[data-pair=table] th")!, kit.querySelector("[data-pair=table] th")!, CELL]);
  out.push(["table td", panel.querySelector("[data-pair=table] td")!, kit.querySelector("[data-pair=table] td")!, CELL]);
  out.push(["table row", panel.querySelector("[data-pair=table] tbody tr")!, kit.querySelector("[data-pair=table] tbody tr")!,
    ["border-bottom-color", "border-bottom-width", "border-bottom-style"]]);
  for (const [sel, props] of PROSE) {
    out.push([`prose ${sel}`, panel.querySelector(`[data-pair=prose] ${sel}`)!, kit.querySelector(`[data-pair=prose] ${sel}`)!, props]);
  }
  return out;
}

/** The element's offset from the top of its `[data-pair]` block, so the layout is compared too. */
const top = (el: Element) => Math.round(el.getBoundingClientRect().top - el.closest("[data-pair]")!.getBoundingClientRect().top);

/** Every compared property that differs, as `<label> <property>: <panel> vs <miniapp>`. */
function diff(root: Element): string[] {
  return pairs(root).flatMap(([label, a, b, props]) => {
    if (!a || !b) return [`${label}: missing element`];
    const sa = getComputedStyle(a);
    const sb = getComputedStyle(b);
    const out = props.filter((p) => sa.getPropertyValue(p) !== sb.getPropertyValue(p))
      .map((p) => `${label} ${p}: ${sa.getPropertyValue(p)} vs ${sb.getPropertyValue(p)}`);
    return top(a) === top(b) ? out : [...out, `${label} top: ${top(a)}px vs ${top(b)}px`];
  });
}

const KIT_HTML = `
  <div class="ag-row" data-pair="button">${BUTTONS.map((v) =>
    `<button type="button" class="ag-button${v === "default" ? "" : ` ag-button--${v}`}">Save</button>`).join("")}</div>
  <div class="ag-row" data-pair="badge">${TONES.map((t) => `<span class="ag-badge ag-badge--${t}">${t}</span>`).join("")}</div>
  <div class="ag-field" data-pair="field"><label class="ag-field__label" for="k-name">Name</label>
    <input class="ag-field__input" id="k-name" aria-describedby="k-name-hint"><span class="ag-field__hint" id="k-name-hint">As on the invoice.</span></div>
  <div class="ag-field" data-pair="select"><label class="ag-field__label" for="k-role">Role</label>
    <select class="ag-field__input" id="k-role"><option>user</option><option>admin</option></select></div>
  <div class="ag-field ag-field--contained" data-pair="contained"><label class="ag-field__label" for="k-city">City</label>
    <input class="ag-field__input" id="k-city"></div>
  <div class="ag-table" data-pair="table"><table><caption class="ag-visually-hidden">Jobs</caption>
    <thead><tr><th scope="col">Name</th></tr></thead><tbody><tr><td>Alpha</td></tr><tr><td>Beta</td></tr></tbody></table></div>
  <div data-pair="prose"><div class="ag-prose">${PROSE_HTML}</div></div>`;

function SideBySide() {
  return (
    <div className="ag-row" style={{ alignItems: "start" }}>
      <section aria-label="Panel components" data-side="panel" className="ag-stack" style={{ width: 420 }}>
        <div className="ag-row" data-pair="button">{BUTTONS.map((v) => <Button key={v} variant={v}>Save</Button>)}</div>
        <div className="ag-row" data-pair="badge">{TONES.map((t) => <StatusBadge key={t} tone={t}>{t}</StatusBadge>)}</div>
        <div data-pair="field"><TextField label="Name" hint="As on the invoice." /></div>
        <div data-pair="select"><SelectField label="Role" value="user" options={ROLES} /></div>
        <div data-pair="contained"><TextField label="City" contained /></div>
        <div data-pair="table">
          <DataTable caption="Jobs" rows={[{ n: "Alpha" }, { n: "Beta" }]} rowKey={(r) => r.n}
            columns={[{ id: "n", header: "Name", cell: (r) => r.n }]} />
        </div>
        <div data-pair="prose"><Markdown>{PROSE_MD}</Markdown></div>
      </section>
      <section aria-label="Miniapp markup" data-side="miniapp" className="ag-stack" style={{ width: 420 }}
        dangerouslySetInnerHTML={{ __html: KIT_HTML }} />
    </div>
  );
}

const meta: Meta = { title: "Acceptance/Mantine parity", render: () => <SideBySide /> };
export default meta;

const play: StoryObj["play"] = async ({ canvasElement }) => {
  // Markdown loads its renderer lazily: compare once the panel side has rendered.
  await waitFor(() => expect(canvasElement.querySelector('[data-side="panel"] [data-pair=prose] table')).not.toBeNull(), { timeout: 10_000 });
  await expect(diff(canvasElement)).toEqual([]);
};

export const Light: StoryObj = { globals: { theme: "light" }, play };
export const Dark: StoryObj = { globals: { theme: "dark" }, play };
