import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { storybookTest } from "@storybook/addon-vitest/vitest-plugin";
import { playwright } from "@vitest/browser-playwright";
import { aliases } from "./panel/vite.config.ts";

const here = (p: string) => fileURLToPath(new URL(p, import.meta.url));

export default defineConfig({
  plugins: [react()],
  // Module panel tests live outside frontend/: point their test-only imports at this workspace.
  resolve: { alias: { ...aliases, "@testing-library/react": here("node_modules/@testing-library/react") } },
  server: { fs: { allow: [here("."), here("../src/agento/modules")] } },
  test: {
    projects: [
      {
        extends: true,
        test: {
          name: "unit",
          environment: "jsdom",
          setupFiles: [here("vitest.setup.ts")],
          include: [
            "packages/**/*.test.{ts,tsx}",
            "panel/**/*.test.{ts,tsx}",
            "../src/agento/modules/*/panel/**/*.test.{ts,tsx}",
            "*.test.ts",
          ],
        },
      },
      {
        extends: true,
        // Every story is a test: it renders, its `play` runs, and the a11y check fails on
        // a violation (`parameters.a11y.test: "error"` in .storybook/preview.tsx).
        plugins: [storybookTest({ configDir: here(".storybook") })],
        test: {
          name: "storybook",
          browser: { enabled: true, headless: true, provider: playwright({}), instances: [{ browser: "chromium" }] },
        },
      },
    ],
  },
});
