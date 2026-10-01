import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
// The one stylesheet both runtimes use (PRD E8 §10.3), from the kit build.
import "@agento/miniapp-kit/agento-ui.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
