// The one source of the visual values. The panel uses it through MantineProvider, and the
// miniapp kit build resolves it into `agento-ui.css` (packages/miniapp-kit/scripts/tokens.ts),
// so a Mantine widget in the panel and an `.ag-*` element in a miniapp use the same values.
// Imported by a Node build script with no path aliases: import only bare packages here.
import { createTheme, type CSSVariablesResolver } from "@mantine/core";

export const agentoTheme = createTheme({
  primaryColor: "blue",
  // Shade 8 is the first blue that keeps white text at WCAG AA (5.0:1; shade 6 is 3.6:1).
  primaryShade: 8,
  fontFamily: "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif",
  fontFamilyMonospace: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  headings: { fontFamily: "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif" },
  defaultRadius: "md",
});

/** Raises only the Mantine colors that fail WCAG AA for text (kit.test.ts checks every pair). */
export const agentoCssVariables: CSSVariablesResolver = () => ({
  variables: {},
  light: {
    "--mantine-color-dimmed": "var(--mantine-color-gray-7)",
    "--mantine-color-error": "var(--mantine-color-red-8)",
    "--mantine-color-green-text": "#237032",
    "--mantine-color-green-light-color": "#237032",
    "--mantine-color-yellow-text": "#8a5700",
    "--mantine-color-yellow-light-color": "#8a5700",
  },
  dark: {
    "--mantine-color-dimmed": "#9a9a9a",
    "--mantine-color-error": "var(--mantine-color-red-4)",
  },
});
