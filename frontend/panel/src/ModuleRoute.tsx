import { Suspense, type ComponentType } from "react";
import type { PanelModule } from "@agento/api";
import { LoadingState } from "@agento/ui";
import { useAvailable } from "./availability";
import { NotAvailable } from "./routes/NotAvailable";

/** A module screen, shown only when the module's probe answers. `Screen` is made once, in moduleRoutes(). */
export function ModuleRoute({ module, Screen }: { module: PanelModule; Screen: ComponentType }) {
  const state = useAvailable(module);
  if (state === "pending") return <LoadingState />;
  if (state === "off") return <NotAvailable />;
  return <Suspense fallback={<LoadingState />}><Screen /></Suspense>;
}
