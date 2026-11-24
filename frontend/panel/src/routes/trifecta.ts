// The lethal trifecta of an unsaved tool draft (PRD §5): the tools the draft turns on, after their
// `requires` chain, together carry all three risk labels. One tool may carry several.
import { RISK_KEYS, type RiskKey } from "@agento/ui";
import type { EnablementItem } from "./enablementTree";

export interface Trifecta { complete: boolean; categories: Record<RiskKey, string[]> }

/** Every item counts, not only the ones a search shows. A tool is on when it and every master up
 *  its `requires` chain are checked; an unknown master or a cycle turns it off. */
export function trifecta(items: EnablementItem[], checked: ReadonlySet<string>): Trifecta {
  const byName = new Map(items.map((i) => [i.name, i]));
  const isOn = (item: EnablementItem) => {
    const seen = new Set<string>();
    for (let at: EnablementItem | undefined = item; at; at = at.requires ? byName.get(at.requires) : undefined) {
      if (!checked.has(at.path) || seen.has(at.name)) return false;
      if (!at.requires) return true;
      seen.add(at.name);
    }
    return false;
  };
  const on = items.filter((i) => i.catalog?.labels.length && isOn(i));
  const categories = Object.fromEntries(RISK_KEYS.map((k) => [k, on.filter((i) => i.catalog!.labels.includes(k)).map((i) => i.name)])) as Trifecta["categories"];
  return { complete: RISK_KEYS.every((k) => categories[k].length > 0), categories };
}
