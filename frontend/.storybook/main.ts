import type { StorybookConfig } from "@storybook/react-vite";
import mantinePreset from "postcss-preset-mantine";
import simpleVars from "postcss-simple-vars";
import { aliases } from "../panel/vite.config.ts";

// The lookbook's CSS modules use Mantine's PostCSS mixins and breakpoint variables (as on
// ui.mantine.dev). Only Storybook needs them: the panel ships no CSS module.
const BREAKPOINTS = { xs: "36em", sm: "48em", md: "62em", lg: "75em", xl: "88em" };
const postcss = {
  plugins: [mantinePreset(), simpleVars({
    variables: Object.fromEntries(Object.entries(BREAKPOINTS).map(([k, v]) => [`mantine-breakpoint-${k}`, v])),
  })],
};

const config: StorybookConfig = {
  stories: ["../packages/ui/src/**/*.stories.tsx", "../lookbook/*.stories.tsx"],
  addons: ["@storybook/addon-a11y", "@storybook/addon-vitest"],
  framework: { name: "@storybook/react-vite", options: {} },
  viteFinal: async (cfg) => ({
    ...cfg,
    css: { ...cfg.css, postcss },
    resolve: { ...cfg.resolve, alias: { ...(cfg.resolve?.alias ?? {}), ...aliases } },
  }),
};

export default config;
