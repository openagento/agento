// The kit's `:root` variables, resolved from the panel's Mantine theme (packages/ui/src/theme.ts),
// so the panel and a miniapp share every value. Mantine names its variables as `var()` chains;
// they are resolved to literals here and emitted under `--ag-*` names, so the agent-facing CSS
// names no vendor and a kit version never changes when Mantine does.
import { DEFAULT_THEME, defaultCssVariablesResolver, mergeMantineTheme } from "@mantine/core";
import { agentoCssVariables, agentoTheme } from "../../ui/src/theme.ts";

type Mode = "light" | "dark";

const theme = mergeMantineTheme(DEFAULT_THEME, agentoTheme);
const base = defaultCssVariablesResolver(theme);
const own = agentoCssVariables(theme);
const VARS: Record<Mode, Record<string, string>> = {
  light: { ...base.variables, ...base.light, ...own.variables, ...own.light },
  dark: { ...base.variables, ...base.dark, ...own.variables, ...own.dark },
};

function resolve(name: string, mode: Mode): string {
  let value = VARS[mode][name];
  for (let ref = /^var\((--[\w-]+)\)$/.exec(value ?? ""); ref; ref = /^var\((--[\w-]+)\)$/.exec(value)) {
    value = VARS[mode][ref[1]];
  }
  if (!value) throw new Error(`Mantine variable ${name} does not resolve in ${mode} mode`);
  return value;
}

/** Each kit color and the Mantine variable it is taken from (one per mode where they differ). */
const SOURCE: Record<string, string | Record<Mode, string>> = {
  background: "--mantine-color-body",
  surface: "--mantine-color-default",
  surfaceHover: "--mantine-color-default-hover",
  surfaceText: "--mantine-color-default-color",
  border: "--mantine-color-default-border",
  // Table rows and card borders: Mantine sets this in the component CSS, not in a theme variable.
  divider: { light: "--mantine-color-gray-3", dark: "--mantine-color-dark-4" },
  text: "--mantine-color-text",
  textMuted: "--mantine-color-dimmed",
  placeholder: "--mantine-color-placeholder",
  link: "--mantine-color-anchor",
  primary: "--mantine-primary-color-filled",
  primaryHover: "--mantine-primary-color-filled-hover",
  primaryText: "--mantine-primary-color-contrast",
  primaryLight: "--mantine-primary-color-light",
  primaryLightHover: "--mantine-primary-color-light-hover",
  primaryLightText: "--mantine-primary-color-light-color",
  error: "--mantine-color-error",
  // The select chevron: Mantine sets this in the Combobox CSS, not in a theme variable.
  chevron: { light: "--mantine-color-gray-6", dark: "--mantine-color-dark-3" },
};

/** The status tones and the Mantine color of each: text, and the `light` variant of a badge. */
export const TONES = { neutral: "gray", info: "blue", success: "green", warning: "yellow", danger: "red" } as const;

function colors(mode: Mode): Record<string, string> {
  const out = Object.fromEntries(Object.entries(SOURCE).map(([k, v]) => [k, resolve(typeof v === "string" ? v : v[mode], mode)]));
  for (const [tone, c] of Object.entries(TONES)) {
    out[tone] = resolve(`--mantine-color-${c}-text`, mode);
    out[`${tone}Light`] = resolve(`--mantine-color-${c}-light`, mode);
    out[`${tone}LightHover`] = resolve(`--mantine-color-${c}-light-hover`, mode);
    out[`${tone}LightText`] = resolve(`--mantine-color-${c}-light-color`, mode);
  }
  return out;
}

export const light = colors("light");
export const dark = colors("dark");

/** Mantine writes sizes as `calc(<n>rem * var(--mantine-scale))`; the kit has no scale. */
const unscaled = (v: string) => v.replace(/^calc\(([\d.]+rem) \* var\(--mantine-scale\)\)$/, "$1");
const pick = (o: Record<string, string>) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, unscaled(v)]));

export const radius = pick(theme.radius);
export const fontSize = pick(theme.fontSizes);
/** The kit's own layout steps for gaps and padding (Mantine has no numeric scale). */
export const space = { 1: "4px", 2: "8px", 3: "12px", 4: "16px", 5: "20px", 6: "24px", 7: "32px", 8: "40px" } as const;

const kebab = (s: string) => s.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());

// Mantine's ComboboxChevron path, so a native select shows the same up-down chevron as a Mantine Select.
const CHEVRON = "M4.93179 5.43179C4.75605 5.60753 4.75605 5.89245 4.93179 6.06819C5.10753 6.24392 5.39245 6.24392 5.56819 6.06819L7.49999 4.13638L9.43179 6.06819C9.60753 6.24392 9.89245 6.24392 10.0682 6.06819C10.2439 5.89245 10.2439 5.60753 10.0682 5.43179L7.81819 3.18179C7.73379 3.0974 7.61933 3.04999 7.49999 3.04999C7.38064 3.04999 7.26618 3.0974 7.18179 3.18179L4.93179 5.43179ZM10.0682 9.56819C10.2439 9.39245 10.2439 9.10753 10.0682 8.93179C9.89245 8.75606 9.60753 8.75606 9.43179 8.93179L7.49999 10.8636L5.56819 8.93179C5.39245 8.75606 5.10753 8.75606 4.93179 8.93179C4.75605 9.10753 4.75605 9.39245 4.93179 9.56819L7.18179 11.8182C7.35753 11.9939 7.64245 11.9939 7.81819 11.8182L10.0682 9.56819Z";
const chevronUrl = (color: string) =>
  `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 15 15'%3E%3Cpath fill='${color.replace("#", "%23")}' fill-rule='evenodd' d='${CHEVRON}'/%3E%3C/svg%3E")`;

function colorBlock(map: Record<string, string>): string {
  return [...Object.keys(map).map((k) => `  --ag-color-${kebab(k)}: ${map[k]};`),
    `  --ag-select-chevron: ${chevronUrl(map.chevron)};`].join("\n");
}

/** The `:root` variable section of `agento-ui.css`: light by default, dark by the OS
 *  preference unless the page says `data-theme="light"`, and dark when it says `dark`. */
export function variablesCss(): string {
  const rest = [
    ...Object.entries(radius).map(([k, v]) => `  --ag-radius-${k}: ${v};`),
    ...Object.entries(space).map(([k, v]) => `  --ag-space-${k}: ${v};`),
    `  --ag-font-family: ${theme.fontFamily};`,
    `  --ag-font-mono: ${theme.fontFamilyMonospace};`,
    ...Object.entries(fontSize).map(([k, v]) => `  --ag-font-size-${k}: ${v};`),
    `  --ag-line-height: ${theme.lineHeights.md};`,
  ].join("\n");
  return [
    `:root {\n  color-scheme: light dark;\n${colorBlock(light)}\n${rest}\n}`,
    `@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {\n${colorBlock(dark).replace(/^/gm, "  ")}\n  }\n}`,
    `:root[data-theme="dark"] {\n${colorBlock(dark)}\n}`,
  ].join("\n\n");
}
