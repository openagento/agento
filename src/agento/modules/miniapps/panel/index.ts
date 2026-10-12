// The miniapps module's panel screens (D-MINIAPP-2). Metadata only: the screens load lazily.
// The probe answers 2xx for a user who holds the `miniapps` or the `miniapps.approve` leaf.
import type { PanelModule } from "@agento/api";

const miniapps: PanelModule = {
  contractVersion: 1,
  id: "miniapps",
  availability: { probe: "/api/miniapps/agent-views" },
  routes: [
    { path: "/apps", nav: "Apps", load: () => import("./AppsPage.tsx") },
    { path: "/apps/:code", load: () => import("./ApprovalPage.tsx") },
  ],
};

export default miniapps;
