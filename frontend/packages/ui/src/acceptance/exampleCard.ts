// The example miniapp's acceptance card, read from the example file itself — never
// re-typed — so the side-by-side test compares the real miniapp markup.
import exampleHtml from "../../../../miniapps/examples/job-status/index.html?raw";

export const EXAMPLE_HTML = exampleHtml;

/** The `<article id="job-card">` element of the example page, as HTML. */
export function exampleCardHtml(): string {
  const doc = new DOMParser().parseFromString(exampleHtml, "text/html");
  const card = doc.getElementById("job-card");
  if (!card) throw new Error("the example miniapp has no #job-card");
  card.removeAttribute("id");
  return card.outerHTML;
}

/** The same card as `<JobStatusCard>` props. */
export const EXAMPLE_PROPS = { title: "Nightly import", state: "running", detail: "Step 2 of 4" } as const;

export const COMPARED = [
  "background-color", "color", "border-top-color", "border-left-color", "border-top-width", "border-left-width",
  "border-radius", "padding-top", "padding-left", "margin-top", "font-family", "font-size", "font-weight", "line-height",
] as const;

/** Walk two element trees in step; return each property that differs, by path. */
export function styleDiff(a: Element, b: Element, path = a.tagName.toLowerCase()): string[] {
  const out: string[] = [];
  if (a.tagName !== b.tagName || a.className !== b.className) out.push(`${path}: ${a.className} vs ${b.className}`);
  const sa = getComputedStyle(a);
  const sb = getComputedStyle(b);
  for (const p of COMPARED) {
    if (sa.getPropertyValue(p) !== sb.getPropertyValue(p)) out.push(`${path} ${p}: ${sa.getPropertyValue(p)} vs ${sb.getPropertyValue(p)}`);
  }
  const ca = [...a.children];
  const cb = [...b.children];
  if (ca.length !== cb.length) out.push(`${path}: ${ca.length} children vs ${cb.length}`);
  ca.forEach((c, i) => { if (cb[i]) out.push(...styleDiff(c, cb[i], `${path} > ${c.tagName.toLowerCase()}`)); });
  return out;
}
