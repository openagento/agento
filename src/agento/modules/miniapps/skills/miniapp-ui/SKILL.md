# Miniapp UI

Use when you write a miniapp page: hand-written HTML over the Agento kit 1.1.0 (`.ag-*` classes, `<ag-*>` elements). Generated from frontend/packages/miniapp-kit; do not edit.

## Page skeleton

Load the kit from the apps origin. Write plain HTML; set every text with `textContent`, never `innerHTML`.

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>My app</title>
  <link rel="stylesheet" href="/_ui/1.1.0/agento-ui.css">
  <script type="module" src="/_ui/1.1.0/agento-ui.js"></script>
</head>
<body class="ag-app">
  <main class="ag-page ag-stack">…</main>
  <script type="module">
    import { createAgentoSdk } from "/_ui/1.1.0/agento-bridge.js";
    const sdk = createAgentoSdk({ panelOrigin: "https://<panel host>" });
    // sdk.callAction("<tool listed in miniapp.json actions>", { ... }) returns a Promise.
  </script>
</body>
</html>
```

Add `miniapp.json` next to `index.html`: `{"schema": 1, "title": "…", "actions": []}`. An action is a declared tool name; it works only after an operator activates the version.

## Layout rules

- The page must work at 320px wide with no sideways page scroll. Put wide tables in `<ag-table>`.
- Under 480px, `.ag-row` wraps; do not set fixed widths.
- From 768px, `.ag-page` gets larger gutters. Design for 320, check 480 and 768.
- Use only the `--ag-*` variables for color. Light and dark modes come from the kit.
- No inline `style`, no other stylesheet, no external script.

## Classes

### `.ag-app`

Page root. Put it on <body>: it sets the font, the text color and the background.

```html
<body class="ag-app"><main class="ag-page">…</main></body>
```

### `.ag-page`

Centered page column with side gutters. Full width at 320px, at most 960px wide.

```html
<main class="ag-page">…</main>
```

### `.ag-stack`

Vertical stack with a gap. `.ag-row` is the horizontal one; it wraps under 480px.

```html
<div class="ag-stack"><div>…</div><div>…</div></div>
<div class="ag-row"><button class="ag-button">A</button><button class="ag-button">B</button></div>
```

### `.ag-page-header`

Page title, an optional description and actions on the right.

```html
<header class="ag-page-header"><div><h1 class="ag-page-header__title">Jobs</h1><p class="ag-page-header__description">Recent runs.</p></div><div class="ag-row">…</div></header>
```

### `.ag-section-header`

Section title inside a page.

```html
<h2 class="ag-section-header">Settings</h2>
```

### `.ag-card`

Bordered surface for one item. Header, body and an optional meta line.
`.ag-card__body--pre` keeps the line breaks of plain text.

```html
<article class="ag-card"><header class="ag-card__head"><h3 class="ag-card__title">Title</h3></header><div class="ag-card__body">Text</div><p class="ag-card__meta">Updated 2 min ago</p></article>
```

### `.ag-job`

Job status card: an `.ag-card` with a state badge. States: pending, published, running, succeeded, failed, blocked.

```html
<article class="ag-card ag-job"><header class="ag-card__head"><h3 class="ag-card__title">Nightly import</h3><span class="ag-badge ag-badge--running">Running</span></header><p class="ag-card__body">Step 2 of 4</p><p class="ag-card__meta">Updated 10:42</p></article>
```

### `.ag-badge`

Short status label. Modifiers: --pending, --published, --running, --succeeded, --failed, --blocked, --neutral, --info. The text says the state; color is not the only signal.

```html
<span class="ag-badge ag-badge--succeeded">Succeeded</span>
```

### `.ag-button`

Button. Modifiers: --primary, --subtle, --danger. Use a real <button>.

```html
<button class="ag-button ag-button--primary" type="button">Save</button>
```

### `.ag-field`

Labelled form field with an optional hint or error. Keep the hint outside the <label> and link it with aria-describedby, so it is not read as part of the name.
A `<select class="ag-field__input">` gets the same box and chevron as a text input; its open list is drawn by the browser.
Modifier --contained puts the label inside the box, in small text above the value.

```html
<div class="ag-field"><label class="ag-field__label" for="name">Name</label><input class="ag-field__input" id="name" name="name" aria-describedby="name-hint"><span class="ag-field__hint" id="name-hint">As on the invoice.</span></div>
<div class="ag-field"><label class="ag-field__label" for="role">Role</label><select class="ag-field__input" id="role" name="role"><option>user</option><option>admin</option></select></div>
<div class="ag-field ag-field--contained"><label class="ag-field__label" for="city">City</label><input class="ag-field__input" id="city" name="city"></div>
```

### `.ag-table`

Data table. Wrap it in `.ag-table` so it scrolls sideways on a narrow screen, or use the `<ag-table>` element.

```html
<div class="ag-table"><table><thead><tr><th>Name</th></tr></thead><tbody><tr><td>Alpha</td></tr></tbody></table></div>
```

### `.ag-state`

Empty, loading and error states. Modifiers: --empty, --loading, --error. Say what happened and what to do.

```html
<div class="ag-state ag-state--empty"><p class="ag-state__title">No jobs yet</p><p class="ag-state__text">A job shows here when it starts.</p></div>
```

### `.ag-code`

Monospace block for code, ids or JSON. Scrolls sideways; never wraps a long token.

```html
<pre class="ag-code">{"ok": true}</pre>
```

### `.ag-prose`

Formatted text: headings, paragraphs, links, lists, inline code, code blocks, tables, quotes and rules. The panel shows an agent's Markdown with the same look.
The kit does not parse Markdown: write the HTML inside `.ag-prose` yourself, and set user text with `textContent`. Not copied: `<mark>`, `<kbd>` and `<details>` keep the browser look.

```html
<div class="ag-prose"><h2>Summary</h2><p>The import <strong>finished</strong>. Run <code>bin/agento replay 42</code>.</p><ul><li>120 rows read</li></ul><pre><code>{"ok": true}</code></pre></div>
<div class="ag-prose"><table><thead><tr><th>Step</th><th>State</th></tr></thead><tbody><tr><td>Read</td><td>done</td></tr></tbody></table><blockquote><p>Next run at 02:00.</p></blockquote></div>
```

### `.ag-list`

A list with no bullets and a gap between items.

```html
<ul class="ag-list"><li>One</li><li>Two</li></ul>
```

### `.ag-muted`

Secondary text.

```html
<p class="ag-muted">Last synced 5 min ago</p>
```

### `.ag-dialog`

Modal dialog on a native <dialog>. Use the `<ag-dialog>` element, which opens and closes it.

```html
<dialog class="ag-dialog"><h2 class="ag-dialog__title">Delete?</h2><p>This cannot be undone.</p><div class="ag-row"><button class="ag-button">Cancel</button></div></dialog>
```

### `.ag-visually-hidden`

Text for screen readers only.

```html
<span class="ag-visually-hidden">Copied</span>
```

## Elements

### `<ag-table>`

Wraps one <table>. It scrolls sideways on a narrow screen, sorts by a header with `data-sort`, and shows `data-empty` when the body has no row.

```html
<ag-table data-empty="No jobs yet"><table><thead><tr><th data-sort>Name</th><th data-sort>State</th></tr></thead><tbody><tr><td>Import</td><td>running</td></tr></tbody></table></ag-table>
```

### `<ag-dialog>`

Wraps one <dialog class="ag-dialog">. A button with `data-ag-open="<id of the ag-dialog>"` opens it; a button inside with `data-ag-close` closes it. Esc closes it.

```html
<button class="ag-button" data-ag-open="confirm">Delete</button><ag-dialog id="confirm"><dialog class="ag-dialog"><h2 class="ag-dialog__title">Delete?</h2><div class="ag-row"><button class="ag-button" data-ag-close>Cancel</button></div></dialog></ag-dialog>
```

### `<ag-copy>`

A copy-to-clipboard button for the `value` attribute.

```html
<ag-copy value="job-123"></ag-copy>
```

### `<ag-json>`

Shows its text content as indented JSON in an `.ag-code` block. Invalid JSON is shown as is.

```html
<ag-json>{"ok":true,"count":2}</ag-json>
```
