// The editor's lethal-trifecta warning (PRD §5), from the unsaved draft. The look is the shared
// `TrifectaAlert` of `@agento/ui`, which the conversation screen uses too.
import { TrifectaAlert as Alert, useT } from "@agento/ui";
import type { Trifecta } from "../trifecta";

export function TrifectaAlert({ categories }: { categories: Trifecta["categories"] }) {
  const t = useT("panel");
  return <Alert title={t("trifecta.title")} categories={categories} intro={t("trifecta.intro")} />;
}
