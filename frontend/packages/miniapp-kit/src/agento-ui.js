// The kit's custom elements (PRD E8 §10.2). Plain classes, no framework, no dependency:
// a miniapp loads this file with <script type="module">. Every text a page gives is set
// with textContent, never innerHTML.

const compare = (a, b) => {
  const na = Number(a);
  const nb = Number(b);
  if (a !== '' && b !== '' && !Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
  return a.localeCompare(b);
};

class AgTable extends HTMLElement {
  connectedCallback() {
    this.classList.add('ag-table');
    const table = this.querySelector('table');
    if (!table) return;
    table.querySelectorAll('th[data-sort]').forEach((th) => {
      if (th.querySelector('button')) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = th.textContent;
      th.textContent = '';
      th.append(button);
      th.setAttribute('aria-sort', 'none');
      // The column is the header's own cell index, not its place among the sortable headers.
      button.addEventListener('click', () => this.sort(th, th.cellIndex));
    });
    this.renderEmpty();
  }

  sort(th, index) {
    const body = this.querySelector('tbody');
    if (!body) return;
    const ascending = th.getAttribute('aria-sort') !== 'ascending';
    this.querySelectorAll('th[data-sort]').forEach((h) => h.setAttribute('aria-sort', 'none'));
    th.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
    const cell = (row) => (row.children[index]?.textContent ?? '').trim();
    const rows = [...body.rows].sort((a, b) => compare(cell(a), cell(b)) * (ascending ? 1 : -1));
    body.append(...rows);
  }

  renderEmpty() {
    const body = this.querySelector('tbody');
    const text = this.getAttribute('data-empty');
    if (!body || !text || body.rows.length > 0) return;
    const row = body.insertRow();
    const td = row.insertCell();
    td.colSpan = this.querySelectorAll('thead th').length || 1;
    td.className = 'ag-state ag-state--empty';
    td.textContent = text;
  }
}

class AgDialog extends HTMLElement {
  connectedCallback() {
    this.dialog = this.querySelector('dialog');
    this.dialog?.addEventListener('click', (e) => {
      if (e.target instanceof Element && e.target.closest('[data-ag-close]')) this.close();
    });
  }

  open() { this.dialog?.showModal(); }

  close() { this.dialog?.close(); }
}

class AgCopy extends HTMLElement {
  connectedCallback() {
    if (this.querySelector('button')) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ag-button ag-button--subtle';
    button.textContent = 'Copy';
    const status = document.createElement('span');
    status.className = 'ag-visually-hidden';
    status.setAttribute('role', 'status');
    button.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(this.getAttribute('value') ?? '');
        status.textContent = 'Copied';
      } catch {
        status.textContent = 'Copy failed';
      }
    });
    this.append(button, status);
  }
}

class AgJson extends HTMLElement {
  connectedCallback() {
    if (this.querySelector('pre')) return;
    const raw = this.textContent ?? '';
    let text = raw;
    try { text = JSON.stringify(JSON.parse(raw), null, 2); } catch { /* shown as is */ }
    const pre = document.createElement('pre');
    pre.className = 'ag-code';
    pre.textContent = text;
    this.replaceChildren(pre);
  }
}

export const ELEMENTS = { 'ag-table': AgTable, 'ag-dialog': AgDialog, 'ag-copy': AgCopy, 'ag-json': AgJson };

export function defineElements(registry = globalThis.customElements) {
  if (!registry) return;
  for (const [name, cls] of Object.entries(ELEMENTS)) {
    if (!registry.get(name)) registry.define(name, cls);
  }
}

document.addEventListener('click', (e) => {
  const opener = e.target instanceof Element ? e.target.closest('[data-ag-open]') : null;
  const target = opener ? document.getElementById(opener.getAttribute('data-ag-open') ?? '') : null;
  if (target instanceof AgDialog) target.open();
});

defineElements();
