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
}

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
  return v as PanelModule;
}
