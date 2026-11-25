import type { StorybookConfig } from "@storybook/react-vite";
import mantinePreset from "postcss-preset-mantine";
import simpleVars from "postcss-simple-vars";
import { fileURLToPath } from "node:url";
import { aliases } from "../panel/vite.config.ts";

// The lookbook's CSS modules use Mantine's PostCSS mixins and breakpoint variables (as on
// ui.mantine.dev). Only Storybook needs them: the panel ships no CSS module.
const BREAKPOINTS = { xs: "36em", sm: "48em", md: "62em", lg: "75em", xl: "88em" };
const postcss = {
  plugins: [mantinePreset(), simpleVars({
    variables: Object.fromEntries(Object.entries(BREAKPOINTS).map(([k, v]) => [`mantine-breakpoint-${k}`, v])),
  })],
};

// The bundled English messages (.storybook/preview.tsx) live in the Python package, outside frontend/.
const FRONTEND_DIR = fileURLToPath(new URL("..", import.meta.url));
const SOURCE_MESSAGES_DIR = fileURLToPath(new URL("../../src/agento/framework/i18n", import.meta.url));

const config: StorybookConfig = {
  stories: ["../packages/ui/src/**/*.stories.tsx", "../lookbook/*.stories.tsx"],
  addons: ["@storybook/addon-a11y", "@storybook/addon-vitest"],
  framework: { name: "@storybook/react-vite", options: {} },
  viteFinal: async (cfg) => ({
    ...cfg,
    css: { ...cfg.css, postcss },
    resolve: { ...cfg.resolve, alias: { ...(cfg.resolve?.alias ?? {}), ...aliases } },
    server: { ...cfg.server, fs: { ...cfg.server?.fs, allow: [...(cfg.server?.fs?.allow ?? [FRONTEND_DIR]), SOURCE_MESSAGES_DIR] } },
  }),
};

export default config;
