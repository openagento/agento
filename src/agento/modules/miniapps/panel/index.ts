// The miniapps module's panel screens (D-MINIAPP-3). Metadata only: the screens load lazily.
// The probe is the catalogue: it answers 2xx for every signed-in user (an empty list without apps).
import type { PanelModule } from "@agento/api";

const miniapps: PanelModule = {
  contractVersion: 1,
  id: "miniapps",
  availability: { probe: "/api/miniapps/apps" },
  routes: [
    { path: "/apps", nav: "Apps", load: () => import("./AppsPage.tsx") },
    { path: "/apps/:code", load: () => import("./AppSettingsPage.tsx") },
  ],
};

export default miniapps;
