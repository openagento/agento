// The editor's lethal-trifecta warning (PRD §5), from the unsaved draft. The look is the shared
// `TrifectaAlert` of `@agento/ui`, which the conversation screen uses too.
import { TrifectaAlert as Alert } from "@agento/ui";
import type { Trifecta } from "../trifecta";

export function TrifectaAlert({ categories }: { categories: Trifecta["categories"] }) {
  return (
    <Alert title="Lethal trifecta" categories={categories}
      intro="With these tools on, an agent can read private data, take in untrusted content and communicate externally. Text in the untrusted content can make it send the private data out. Turn off every tool of one group to break the trifecta." />
  );
}
