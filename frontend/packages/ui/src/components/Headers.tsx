import { ActionIcon } from "@mantine/core";
import { Pencil } from "lucide-react";
import { useState, type ReactNode } from "react";
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
 *  `onSave` gets the trimmed text only when it changed (an empty text is the caller's "reset"). */
export function EditableTitle({ value, label = "Title", maxLength, onSave }: {
  value: string; label?: string; maxLength?: number; onSave: (value: string) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  if (draft !== null) {
    const done = () => { setDraft(null); if (draft.trim() !== value) onSave(draft.trim()); };
    return (
      // eslint-disable-next-line jsx-a11y/no-autofocus -- the user just asked to edit this field
      <input className={css.input} aria-label={label} autoFocus value={draft} maxLength={maxLength}
        onChange={(e) => setDraft(e.target.value)} onBlur={done}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); e.currentTarget.blur(); }
          if (e.key === "Escape") setDraft(null);
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
