# Mantine look for the panel and the miniapp kit, plus a lookbook

Status: approved by the owner on 2026-10-02 (see Settled). Branch `feature/E8-composable-frontend`.

## Goal

The panel and the miniapps look like the Mantine examples on ui.mantine.dev. The Mantine theme is the
one source of the visual values; the miniapp kit is generated from it. The ui.mantine.dev patterns are
copied into a Storybook lookbook.

## Verified facts (PLN-1)

| Fact | Proof |
|---|---|
| Panel components are plain elements with `.ag-*` classes; Mantine only for shell, modal, tabs, notifications | `DECISIONS.md 2026-10-01 E8`, `frontend/eslint.config.js` (Styles API ban) |
| `tokens.ts` builds `agento-ui.css` and the Mantine theme | `frontend/packages/miniapp-kit/scripts/build-kit.ts:13`, `frontend/packages/ui/src/theme.ts:4` |
| Mantine resolves its CSS variables from a theme in Node, at build time | `defaultCssVariablesResolver(mergeMantineTheme(DEFAULT_THEME, t))` in `@mantine/core` 9.6.3: 224 shared + 129 dark variables (spike, 2026-10-02) |
| The resolver returns `var()` chains, not literals | same spike: `--mantine-color-body = var(--mantine-color-dark-7)` |
| Mantine defaults fail WCAG AA in places: light `dimmed` 3.32, dark `dimmed` on `default` 3.53, light green/yellow light-variant 3.81/2.69, light filled blue-6 with white 3.55 | contrast spike, 2026-10-02 |
| The kit build script may import Mantine; kit `src/` may not | `frontend/boundaries.test.ts:62` excludes `scripts/` |
| `node --experimental-strip-types` ignores tsconfig `paths` | Node docs (type stripping does no resolution) |
| Kit 1.0.0 has a committed released copy; the build fails on a byte change | `build-kit.ts:71-77`, `git ls-files frontend/packages/miniapp-kit/released` |
| Every story runs as a test with axe at `error` | `frontend/vitest.config.ts:91`, `frontend/.storybook/preview.tsx` |
| ui.mantine.dev is MIT, 124 patterns in 24 categories, at commit `7bce6d7` (2026-09-22) | `LICENCE`, `lib/*/attributes.json` |

## Scope

In:

1. **Theme is the source.** `packages/ui/src/theme.ts`: `primaryColor: "blue"`, `primaryShade: 8`
   (AA with white text), Inter, and `agentoCssVariables`, a `cssVariablesResolver` that raises only
   the colors that fail AA. Every `MantineProvider` uses both.
2. **Kit generated from the theme.** `packages/miniapp-kit/scripts/tokens.ts` resolves the Mantine
   variables to literals and emits them under the existing `--ag-*` names (no vendor name in the
   agent-facing CSS). `src/tokens.ts` and the `@agento/miniapp-kit/tokens` alias go.
   `components.css` is rewritten to the Mantine look: inputs, buttons, badges, tables, cards.
3. **`@agento/ui` on Mantine** for `Button`, `StatusBadge`, `TextField`, `SelectField` and `DataTable`
   (Mantine `Button`, `Badge`, `TextInput`, `NativeSelect`, `Table`). Their props stay, so no screen
   changes. `Card`, `JobStatusCard`, headers, states and code blocks stay `.ag-*`.
4. **Parity test.** The acceptance stories compare the computed styles of each Mantine component
   with its `.ag-*` twin in light and dark, by element role, not by tree shape.
5. **Lookbook.** `frontend/lookbook/`: the ui.mantine.dev patterns copied 1:1 with the MIT notice,
   one story file per category, under `Lookbook/` in Storybook. Tagged `!test`: they render in
   `npm run storybook` but are not tests (third-party code with remote images). Lint skips the
   directory as vendored code.
6. Kit 1.0.0 is re-cut (nothing is released), docs and DECISIONS updated.

Out:

- ~~The 6 dnd/carousel/dropzone patterns~~ and ~~the "label inside the input" variant~~: done in the follow-up below.
- Promoting a lookbook pattern into `@agento/ui`: done when a screen needs it.

## Settled (PLN-4)

- "skopiujmy to jednak wszystko dla bycia spojnym" — owner, 2026-10-02 (copy all patterns).
- "ok, lookbook w Storybooku, rób cały plan" — owner, 2026-10-02 (lookbook in Storybook, the whole plan:
  theme as source, kit generated, `@agento/ui` on Mantine, parity test).
- "nic nie jest jeszcze wydane" — owner, 2026-10-02 (kit 1.0.0 may be re-cut).

## Follow-up, 2026-10-02 (owner review of the panel)

- "selecr, dropdown looks invalid": `SelectField` is now a Mantine `Select`, since a native list is drawn by the OS.
- "we dont use apparently navbars for left navigation, while we should": the shell uses the `NavbarSimple` layout.
- "user list should use this pattern i think": the user list uses `UsersRolesTable` (avatar, role select in the row).
- Found in the work: `DataTable` remounted every cell on each render, so an open select closed; fixed and tested.
- "I think we should do above" (the Out list): the 6 dnd/carousel/dropzone patterns are copied (7 devDependencies);
  the kit styles `<select class="ag-field__input">` with the Mantine chevron and adds `.ag-field--contained`, with a
  `contained` prop on `TextField`/`SelectField`; the parity story compares both. Kit 1.0.0 re-cut again.
