// The versioned_artifacts module's panel screens (D-MINIAPP-2). Metadata only: the screens
// load lazily. The probe answers 403 without the leaf `admin.artifacts`, so the entry is hidden.
import type { PanelModule } from "@agento/api";

const versionedArtifacts: PanelModule = {
  contractVersion: 1,
  id: "versioned_artifacts",
  availability: { probe: "/api/versioned_artifacts/artifacts" },
  routes: [
    { path: "/artifacts", nav: "Artifacts", load: () => import("./ArtifactsPage.tsx") },
    { path: "/artifacts/:code", load: () => import("./ArtifactPage.tsx") },
  ],
};

export default versionedArtifacts;
