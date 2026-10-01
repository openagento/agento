// The panel build (PRD E8 §5): the output lands in the Python package, which the wheel
// ships and proxy mounts at /srv/panel. Dev (§6): the page is served through proxy, so the
// session cookie and the Origin check are the production ones.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";

// Writes every bundled module id per chunk, for the bundle-budget test (boundaries.test.ts).
// Kept beside the source, not in the shipped output.
const bundleReport = (): Plugin => ({
  name: "agento-bundle-report",
  apply: "build",
  generateBundle(_, bundle) {
    const chunks = Object.values(bundle).flatMap((c) => (c.type === "chunk"
      ? [{ file: c.fileName, entry: c.isEntry, modules: c.moduleIds }] : []));
    writeFileSync(here("bundle-modules.json"), JSON.stringify(chunks));
  },
});

const here = (p: string) => fileURLToPath(new URL(p, import.meta.url));
const modules = here("../../src/agento/modules");
// The panel and Storybook load the kit stylesheet of the CURRENT kit version, so a version
// bump moves them with it.
const KIT_VERSION: string = JSON.parse(readFileSync(here("../packages/miniapp-kit/package.json"), "utf8")).version;

export const aliases = {
  "@agento/ui": here("../packages/ui/src/index.ts"),
  "@agento/api": here("../packages/api/src/index.ts"),
  "@agento/miniapp-kit/agento-ui.css": here(`../packages/miniapp-kit/dist/${KIT_VERSION}/agento-ui.css`),
  "@agento/miniapp-kit/tokens": here("../packages/miniapp-kit/src/tokens.ts"),
  "@agento/miniapp-bridge": `${modules}/miniapps/sdk/bridge.js`,
  // A core module's panel/ lives outside frontend/, where Node resolution cannot find
  // frontend/node_modules: point the one runtime it needs at the workspace copy.
  react: here("../node_modules/react"),
  "react-dom": here("../node_modules/react-dom"),
  "react-router": here("../node_modules/react-router"),
};

export default defineConfig({
  root: here("."),
  plugins: [react(), bundleReport()],
  resolve: { alias: aliases },
  build: {
    outDir: here("../../src/agento/framework/web/panel"),
    emptyOutDir: true,
    assetsDir: "assets",
  },
});
