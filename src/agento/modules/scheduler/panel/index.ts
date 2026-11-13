// The scheduler module's panel screens (AG-67, PRD §11). Metadata only: the screens load lazily.
// The probe answers 403 without a scheduler leaf or a share, so the entry is hidden.
import type { PanelModule } from "@agento/api";

const scheduler: PanelModule = {
  contractVersion: 1,
  id: "scheduler",
  availability: { probe: "/api/scheduler/schedules" },
  routes: [
    { path: "/schedules", nav: "Schedules", load: () => import("./SchedulesPage.tsx") },
    { path: "/schedules/:id", load: () => import("./ScheduleForm.tsx") },
  ],
};

export default scheduler;
