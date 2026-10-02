// The conversation module's panel screens (PRD E8 §7, §9). Metadata only: the screen
// loads lazily, so the registry generator can read this file without React.
import type { PanelModule } from "@agento/api";

const conversation: PanelModule = {
  contractVersion: 1,
  id: "conversation",
  availability: { probe: "/api/conversation/threads" },
  routes: [
    { path: "/conversations", nav: "Conversations", load: () => import("./ConversationsPage.tsx") },
    { path: "/conversations/:threadId", load: () => import("./ConversationsPage.tsx") },
  ],
};

export default conversation;
