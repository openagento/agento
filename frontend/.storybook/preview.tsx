import "@mantine/core/styles.css";
import "@agento/miniapp-kit/agento-ui.css";
// The lookbook (frontend/lookbook): the Mantine logo in its headers, and the carousel and dropzone patterns.
import "@mantinex/mantine-logo/styles.css";
import "@mantine/carousel/styles.css";
import "@mantine/dropzone/styles.css";
import type { Preview } from "@storybook/react-vite";
import { AgentoUiProvider } from "../packages/ui/src/Provider";

const preview: Preview = {
  globalTypes: {
    theme: {
      description: "Color scheme",
      toolbar: { title: "Theme", items: ["light", "dark"], dynamicTitle: true },
    },
  },
  initialGlobals: { theme: "light" },
  parameters: { a11y: { test: "error" }, layout: "padded" },
  decorators: [
    (Story, ctx) => {
      const scheme = ctx.globals.theme === "dark" ? "dark" : "light";
      document.documentElement.dataset.theme = scheme;
      document.body.className = "ag-app";
      return (
        <AgentoUiProvider forceColorScheme={scheme}>
          <Story />
        </AgentoUiProvider>
      );
    },
  ],
};

export default preview;
