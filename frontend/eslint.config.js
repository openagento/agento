import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import globals from "globals";

const NOT_SHIPPED = ["**/*.stories.tsx", "**/*.test.{ts,tsx}", "**/testing.tsx", "packages/ui/src/acceptance/**"];
const ENGLISH_SOURCE_OF_DERIVED_KEYS = "*/panel/index.ts";
const LETTER = "/\\p{L}/u";
const TEXT_PROP = "/^(label|title|placeholder|aria-label|description|caption|hint|message|alt|text|header|summary|tooltip|empty\\w*|\\w+(Label|Title|Text))$/";
const USE_A_KEY = "A text a user reads comes from a catalog: useT(namespace), then t(\"key\") (docs/development/frontend.md, Languages).";
const BRANCH = ":matches(ConditionalExpression, LogicalExpression)";
const TEXT_VALUES = [`Literal[value=${LETTER}]`, `TemplateLiteral > TemplateElement[value.raw=${LETTER}]`];
const HOLDERS = [
  "JSXElement > JSXExpressionContainer", "JSXFragment > JSXExpressionContainer",
  `JSXAttribute[name.name=${TEXT_PROP}] > JSXExpressionContainer`, `Property[key.name=${TEXT_PROP}]`,
];
const LITERAL_TEXT = [
  `JSXText[value=${LETTER}]`,
  `JSXAttribute[name.name=${TEXT_PROP}] > Literal[value=${LETTER}]`,
  ...HOLDERS.flatMap((holder) => TEXT_VALUES.flatMap((value) => [`${holder} > ${value}`, `${holder} > ${BRANCH} > ${value}`])),
].map((selector) => ({ selector, message: USE_A_KEY }));
const TEXT_AS_KEY = {
  selector: `JSXAttribute[name.name='key'] MemberExpression[property.name=${TEXT_PROP}]`,
  message: "A React key is a stable id, not a text a user reads: a translation changes it, and two texts can be equal (UI-6).",
};

const TRANSLATION = "CallExpression[callee.name='t']";
const WHOLE_SENTENCES = [
  { selector: `ConditionalExpression[test.operator=/^[!=]==?$/][test.right.value=1] > :matches(Literal[value=${LETTER}], TemplateLiteral, ${TRANSLATION})`,
    message: "Do not choose a plural in code: use a plural message, t(\"key\", { count }) (docs/development/i18n.md)." },
  ...[`BinaryExpression[operator='+'] > ${TRANSLATION}`, `TemplateLiteral > ${TRANSLATION} ~ ${TRANSLATION}`,
    `TemplateLiteral:has(> TemplateElement[value.cooked=${LETTER}]) > ${TRANSLATION}`].map((selector) => ({
    selector, message: "Do not join translated parts: one message is one sentence, with {param} holes (docs/development/i18n.md).",
  })),
];
const CATALOG_RULES = [...LITERAL_TEXT, TEXT_AS_KEY, ...WHOLE_SENTENCES];

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
    files: ["packages/ui/src/**/*.{ts,tsx}", "panel/src/**/*.{ts,tsx}", "*/panel/**/*.{ts,tsx}"],
    ignores: [...NOT_SHIPPED, ENGLISH_SOURCE_OF_DERIVED_KEYS],
    rules: { "no-restricted-syntax": ["error", ...CATALOG_RULES] },
  },
  {
    // A component gets its look from the theme only (theme.ts, which also generates agento-ui.css):
    // a style or Styles API override would be a value the miniapp kit cannot follow.
    files: ["packages/ui/**/*.tsx"],
    ignores: NOT_SHIPPED,
    rules: {
      "no-restricted-syntax": ["error", ...CATALOG_RULES,
        { selector: "JSXAttribute[name.name='styles']", message: "Use an .ag-* class from agento-ui.css, not the Styles API." },
        { selector: "JSXAttribute[name.name='style']", message: "Use an .ag-* class from agento-ui.css, not an inline style." }],
    },
  },
);
