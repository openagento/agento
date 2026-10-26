import { ActionIcon } from "@mantine/core";
import { Pencil } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import css from "./Editable.module.css";

export function PageHeader({ title, description, actions }: {
  title: ReactNode; description?: ReactNode; actions?: ReactNode;
}) {
  return (
    <header className="ag-page-header">
      <div>
        <h1 className="ag-page-header__title">{title}</h1>
        {description && <p className="ag-page-header__description">{description}</p>}
      </div>
      {actions && <div className="ag-row">{actions}</div>}
    </header>
  );
}

export function SectionHeader({ children }: { children: ReactNode }) {
  return <h2 className="ag-section-header">{children}</h2>;
}

/** A title edited in place: a pencil shows on hover; Enter or leaving the field saves, Escape cancels.
 *  `onSave` gets the trimmed text only when it changed (an empty text is the caller's "reset").
 *
 *  `startEditing` mounts it already in the field, for a caller that opened the edit itself (the
 *  Rename item of a thread's row menu); `onDone` then tells that caller the edit is over. */
export function EditableTitle({ value, label = "Title", maxLength, onSave, startEditing = false, onDone }: {
  value: string; label?: string; maxLength?: number; onSave: (value: string) => void;
  startEditing?: boolean; onDone?: () => void;
}) {
  const [draft, setDraft] = useState<string | null>(startEditing ? value : null);
  // A menu that opened this field returns focus to its own trigger as it closes, which
  // blurs the input the instant it mounts and would close the rename before the user types
  // (seen in a real browser; jsdom restores no focus, so a jsdom test cannot see it). Such a
  // blur hands focus to another element in that first moment: it is the menu's, not the
  // user's, so take the focus back. A plain blur (no new focus target) still ends the edit.
  const opened = useRef(0);
  if (draft !== null) {
    const done = () => { setDraft(null); onDone?.(); if (draft.trim() !== value) onSave(draft.trim()); };
    return (
      // eslint-disable-next-line jsx-a11y/no-autofocus -- the user just asked to edit this field
      <input className={css.input} aria-label={label} autoFocus value={draft} maxLength={maxLength}
        onChange={(e) => setDraft(e.target.value)}
        ref={(el) => { if (el && !opened.current) opened.current = Date.now(); }}
        onBlur={(e) => {
          if (e.relatedTarget && Date.now() - opened.current < 400) { e.currentTarget.focus(); return; }
          done();
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); e.currentTarget.blur(); }
          if (e.key === "Escape") { setDraft(null); onDone?.(); }
        }} />
    );
  }
  return (
    <span className={css.editable}>
      {value}
      <ActionIcon variant="subtle" color="gray" size="sm" className={css.pencil} aria-label={`Edit ${label.toLowerCase()}`}
        onClick={() => setDraft(value)}><Pencil size={14} /></ActionIcon>
    </span>
  );
}
