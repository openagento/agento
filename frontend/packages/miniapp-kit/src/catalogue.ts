// What each kit custom element does, for the generated authoring skill (PRD E8 §10.4).
// A test checks that this list and the elements `agento-ui.js` defines are the same set.

export const CATALOGUE: Record<string, { description: string; example: string }> = {
  'ag-table': {
    description:
      'Wraps one <table>. It scrolls sideways on a narrow screen, sorts by a header with `data-sort`, '
      + 'and shows `data-empty` when the body has no row.',
    example:
      '<ag-table data-empty="No jobs yet"><table><thead><tr><th data-sort>Name</th><th data-sort>State</th></tr></thead>'
      + '<tbody><tr><td>Import</td><td>running</td></tr></tbody></table></ag-table>',
  },
  'ag-dialog': {
    description:
      'Wraps one <dialog class="ag-dialog">. A button with `data-ag-open="<id of the ag-dialog>"` opens it; '
      + 'a button inside with `data-ag-close` closes it. Esc closes it.',
    example:
      '<button class="ag-button" data-ag-open="confirm">Delete</button>'
      + '<ag-dialog id="confirm"><dialog class="ag-dialog"><h2 class="ag-dialog__title">Delete?</h2>'
      + '<div class="ag-row"><button class="ag-button" data-ag-close>Cancel</button></div></dialog></ag-dialog>',
  },
  'ag-copy': {
    description: 'A copy-to-clipboard button for the `value` attribute.',
    example: '<ag-copy value="job-123"></ag-copy>',
  },
  'ag-json': {
    description: 'Shows its text content as indented JSON in an `.ag-code` block. Invalid JSON is shown as is.',
    example: '<ag-json>{"ok":true,"count":2}</ag-json>',
  },
};
