// The panel module contract (PRD E8 §7). A CORE module ships `panel/index.ts` whose
// default export is a `PanelModule`; `frontend/panel/scripts/gen-registry.ts` collects
// them at build time. A user or PyPI module never ships panel JavaScript.
import type { ComponentType } from "react";

export const PANEL_CONTRACT_VERSION = 1;

export interface PanelRoute {
  /** Path under the panel, e.g. "/conversations" or "/conversations/:id". */
  path: string;
  /** Present → a navigation entry with this label. */
  nav?: string;
  load: () => Promise<{ default: ComponentType }>;
}

export interface PanelModule {
  contractVersion: 1;
  /** The module's directory name under src/agento/modules/. */
  id: string;
  routes: PanelRoute[];
  /** A GET route the module itself owns. 2xx → the module is on; anything else → hidden.
   *  A disabled module's routes answer 404, so this fails closed. */
  availability: { probe: string };
  /** A link from a job's detail to this module's page about the job. `api` is a GET path under the
   *  module's own /api/<id>/ with one `{job}` (the job id); it answers `{ id }`, and only a 2xx shows
   *  the link. `to` is the panel path with one `{id}` (that answer's id). */
  jobLink?: JobLink;
}

export interface JobLink { label: string; api: string; to: string }

export class RegistryContractError extends Error {
  constructor(module: string, detail: string) {
    super(`${module}: ${detail}`);
    this.name = "RegistryContractError";
  }
}

/** Throws `RegistryContractError` unless `m` is a contract-1 module named `dir`. */
export function assertPanelModule(dir: string, m: unknown): PanelModule {
  const v = (m ?? {}) as Partial<PanelModule>;
  if (v.contractVersion !== PANEL_CONTRACT_VERSION) {
    throw new RegistryContractError(dir, `unknown contractVersion ${String(v.contractVersion)}`);
  }
  if (v.id !== dir) throw new RegistryContractError(dir, `id ${String(v.id)} is not the module directory`);
  if (!Array.isArray(v.routes) || !v.routes.length) throw new RegistryContractError(dir, "no routes");
  const probe = v.availability?.probe;
  if (typeof probe !== "string" || !probe.startsWith(`/api/${dir}/`)) {
    throw new RegistryContractError(dir, `availability.probe must be a GET under /api/${dir}/`);
  }
  if (v.jobLink !== undefined) assertJobLink(dir, v.jobLink);
  return v as PanelModule;
}

const ORIGIN = "https://panel.invalid";
const once = (text: string, part: string) => text.split(part).length === 2;

/** Checked by what the template does, not how it is spelled: it is resolved for two job ids, and the
 *  two URLs must differ in exactly one whole path segment, `1` against `2`, after the module prefix.
 *  So a slot that a later `..` drops, a second slot the URL parser makes (a tab, `%7B`), a slot
 *  inside another segment (`job-{id}`, or `/api/x{job}/` for module `x1`) are all refused. */
function assertJobLink(dir: string, link: Partial<JobLink>) {
  const fail = (detail: string) => { throw new RegistryContractError(dir, `jobLink: ${detail}`); };
  const { label, api, to } = link;
  if (typeof label !== "string" || !label) fail("label must be text");
  for (const [name, template, slot] of [["api", api, "{job}"], ["to", to, "{id}"]] as const) {
    if (typeof template !== "string") return fail(`${name} must be a path`);
    if (!template.startsWith("/") || template.startsWith("//")) fail(`${name} must start with one /`);
    if (/[\\?#]/.test(template)) fail(`${name} must not hold \\, ? or #`);
    if (!once(template, slot)) fail(`${name} must hold ${slot} once`);
    const [one, two] = ["1", "2"].map((id) => new URL(template.replace(slot, id), ORIGIN));
    if (one.origin !== ORIGIN || two.origin !== ORIGIN) fail(`${name} must stay on the panel origin`);
    const prefix = name === "api" ? `/api/${dir}/` : "/";
    if (!one.pathname.startsWith(prefix)) fail(`api must be a GET under /api/${dir}/`);
    const [a, b] = [one.pathname.split("/"), two.pathname.split("/")];
    const differ = a.flatMap((part, i) => (part === b[i] ? [] : [i]));
    const first = prefix.split("/").length - 1; // the first segment after the prefix
    if (a.length !== b.length || differ.length !== 1 || differ[0] < first || a[differ[0]] !== "1" || b[differ[0]] !== "2") {
      fail(`${name} must keep ${slot} as one whole path segment${name === "api" ? ` after /api/${dir}/` : ""}`);
    }
  }
}
