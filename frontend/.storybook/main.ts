import type { StorybookConfig } from "@storybook/react-vite";
import { aliases } from "../panel/vite.config.ts";

const config: StorybookConfig = {
  stories: ["../packages/ui/src/**/*.stories.tsx"],
  addons: ["@storybook/addon-a11y", "@storybook/addon-vitest"],
  framework: { name: "@storybook/react-vite", options: {} },
  viteFinal: async (cfg) => ({ ...cfg, resolve: { ...cfg.resolve, alias: { ...(cfg.resolve?.alias ?? {}), ...aliases } } }),
};

export default config;
