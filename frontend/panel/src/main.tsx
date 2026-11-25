import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
// The one stylesheet both runtimes use (PRD E8 §10.3), from the kit build.
import "@agento/miniapp-kit/agento-ui.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { installSourceMessages } from "@agento/ui";
import { App } from "./App";
import { SOURCE_MESSAGES } from "./generated/messages";

installSourceMessages(SOURCE_MESSAGES);

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
