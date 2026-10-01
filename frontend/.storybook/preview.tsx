import "@mantine/core/styles.css";
import "@agento/miniapp-kit/agento-ui.css";
import { MantineProvider } from "@mantine/core";
import type { Preview } from "@storybook/react-vite";
import { agentoTheme } from "../packages/ui/src/theme";

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
        <MantineProvider theme={agentoTheme} forceColorScheme={scheme}>
          <Story />
        </MantineProvider>
      );
    },
  ],
};

export default preview;
