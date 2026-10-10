import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import globals from "globals";

export default tseslint.config(
  { ignores: ["**/dist/**", "node_modules/**", "panel/src/generated/**", "packages/miniapp-kit/released/**", "storybook-static/**", "lookbook/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx,js}"],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    plugins: { "react-hooks": reactHooks, "jsx-a11y": jsxA11y },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
  {
    // A component gets its look from the theme only (theme.ts, which also generates agento-ui.css):
    // a style or Styles API override would be a value the miniapp kit cannot follow.
    files: ["packages/ui/**/*.tsx"],
    ignores: ["**/*.stories.tsx"],
    rules: {
      "no-restricted-syntax": ["error",
        { selector: "JSXAttribute[name.name='styles']", message: "Use an .ag-* class from agento-ui.css, not the Styles API." },
        { selector: "JSXAttribute[name.name='style']", message: "Use an .ag-* class from agento-ui.css, not an inline style." }],
    },
  },
);
