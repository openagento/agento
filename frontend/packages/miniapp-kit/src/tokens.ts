// The one token source (PRD E8 §10.3). `agento-ui.css` is generated from it, and the
// panel's Mantine theme is built from it, so the panel and a miniapp share every value.

export type ColorName =
  | "background" | "surface" | "surfaceElevated" | "text" | "textMuted" | "border"
  | "primary" | "primaryText" | "success" | "warning" | "error" | "info";

/** The Mantine `agento` scale; `light.primary` is shade 7, `dark.primary` is shade 3. */
export const agentoScale = [
  "#eef2ff", "#e0e7ff", "#c7d2fe", "#a5b4fc", "#818cf8",
  "#6366f1", "#4f46e5", "#4338ca", "#3730a3", "#312e81",
] as const;

export const light: Record<ColorName, string> = {
  background: "#f7f8fa",
  surface: "#ffffff",
  surfaceElevated: "#ffffff",
  text: "#111827",
  textMuted: "#4b5563",
  border: "#d1d5db",
  primary: agentoScale[7],
  primaryText: "#ffffff",
  success: "#15803d",
  warning: "#a16207",
  error: "#b91c1c",
  info: "#1d4ed8",
};

export const dark: Record<ColorName, string> = {
  background: "#0f1115",
  surface: "#171a21",
  surfaceElevated: "#1f2330",
  text: "#e5e7eb",
  textMuted: "#9ca3af",
  border: "#374151",
  primary: agentoScale[3],
  primaryText: "#111827",
  success: "#4ade80",
  warning: "#facc15",
  error: "#f87171",
  info: "#93c5fd",
};

export const radius = { sm: "4px", md: "8px", lg: "12px" } as const;

export const space = {
  1: "4px", 2: "8px", 3: "12px", 4: "16px", 5: "20px", 6: "24px", 7: "32px", 8: "40px",
} as const;

export const font = {
  family: "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif",
  mono: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  size: { sm: "13px", md: "14px", lg: "16px", xl: "20px" },
} as const;

const kebab = (s: string) => s.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());

/** `--ag-color-<name>` for a color token. */
export const colorVar = (name: ColorName) => `--ag-color-${kebab(name)}`;

function colorBlock(colors: Record<ColorName, string>): string {
  return (Object.keys(colors) as ColorName[]).map((k) => `  ${colorVar(k)}: ${colors[k]};`).join("\n");
}

/** The `:root` variable section of `agento-ui.css`: light by default, dark by the OS
 *  preference unless the page says `data-theme="light"`, and dark when it says `dark`. */
export function variablesCss(): string {
  const rest = [
    ...Object.entries(radius).map(([k, v]) => `  --ag-radius-${k}: ${v};`),
    ...Object.entries(space).map(([k, v]) => `  --ag-space-${k}: ${v};`),
    `  --ag-font-family: ${font.family};`,
    `  --ag-font-mono: ${font.mono};`,
    ...Object.entries(font.size).map(([k, v]) => `  --ag-font-size-${k}: ${v};`),
  ].join("\n");
  return [
    `:root {\n  color-scheme: light dark;\n${colorBlock(light)}\n${rest}\n}`,
    `@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {\n${colorBlock(dark).replace(/^/gm, "  ")}\n  }\n}`,
    `:root[data-theme="dark"] {\n${colorBlock(dark)}\n}`,
  ].join("\n\n");
}
