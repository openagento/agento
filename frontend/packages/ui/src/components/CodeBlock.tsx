import { CopyButton } from "./CopyButton";

export function CodeBlock({ code, copy = false }: { code: string; copy?: boolean }) {
  return (
    <div className="ag-stack">
      <pre className="ag-code">{code}</pre>
      {copy && <div className="ag-row"><CopyButton value={code} /></div>}
    </div>
  );
}

/** Indented JSON. React escapes the text; nothing is rendered as HTML. */
export function JsonViewer({ value }: { value: unknown }) {
  let text: string;
  try { text = JSON.stringify(value, null, 2) ?? String(value); } catch { text = String(value); }
  return <pre className="ag-code">{text}</pre>;
}
