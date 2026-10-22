// The one source of the visual values. The panel uses it through MantineProvider, and the
// miniapp kit build resolves it into `agento-ui.css` (packages/miniapp-kit/scripts/tokens.ts),
// so a Mantine widget in the panel and an `.ag-*` element in a miniapp use the same values.
// Imported by a Node build script with no path aliases: import only bare packages here.
import { createTheme, type CSSVariablesResolver, type MantineColorsTuple } from "@mantine/core";

// The neutral scales. Mantine's own `gray` and `dark` carry a blue hue (`#868e96` is
// `hsl(210 8% 55%)`); a chat surface reads as warm-neutral, so these are near-zero
// saturation. The indices are Mantine's: `gray-4`/`dark-4` is the border, `gray-0`/`dark-6`
// the surface, `dark-7` the dark body, `gray-7`/`dark-0` the text.
const GRAY: MantineColorsTuple = [
  "#f9f9f9", "#f4f4f4", "#ececec", "#e3e3e3", "#cdcdcd",
  "#b4b4b4", "#8f8f8f", "#676767", "#424242", "#2f2f2f",
];
const DARK: MantineColorsTuple = [
  "#f5f5f5", "#ececec", "#d4d4d4", "#9b9b9b", "#3f3f3f",
  "#2f2f2f", "#282828", "#212121", "#1a1a1a", "#0f0f0f",
];

export const agentoTheme = createTheme({
  primaryColor: "blue",
  colors: { gray: GRAY, dark: DARK },
  // Shade 8 is the first blue that keeps white text at WCAG AA (5.0:1; shade 6 is 3.6:1).
  primaryShade: 8,
  fontFamily: "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif",
  fontFamilyMonospace: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  headings: { fontFamily: "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif" },
  // Soft corners everywhere, as the chat apps do: a `lg` button, card, input and bubble.
  defaultRadius: "lg",
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
    // The green status badge (SUCCESS, OK, current): a solid green with near-black text (9:1).
    "--mantine-color-green-light": "#30d158",
    "--mantine-color-green-light-color": "#06240f",
  },
});
