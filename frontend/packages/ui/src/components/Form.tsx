import { useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";

export function FormSection({ title, children, onSubmit, error }: {
  title: string; children: ReactNode; onSubmit: () => void; error?: string | null;
}) {
  return (
    <form className="ag-stack" onSubmit={(e) => { e.preventDefault(); onSubmit(); }}>
      <fieldset className="ag-stack">
        <legend className="ag-section-header">{title}</legend>
        {children}
      </fieldset>
      {error && <p className="ag-field__error" role="alert">{error}</p>}
    </form>
  );
}

type FieldProps = { label: string; hint?: string; error?: string | null };

export function TextField({ label, hint, error, ...input }: FieldProps & InputHTMLAttributes<HTMLInputElement>) {
  const id = useId();
  return (
    <div className="ag-field">
      <label className="ag-field__label" htmlFor={id}>{label}</label>
      <input id={id} className="ag-field__input" aria-invalid={error ? true : undefined}
        aria-describedby={error || hint ? `${id}-d` : undefined} {...input} />
      {(error || hint) && <span id={`${id}-d`} className={error ? "ag-field__error" : "ag-field__hint"}>{error ?? hint}</span>}
    </div>
  );
}

export function SelectField({ label, hint, error, options, ...select }: FieldProps
  & SelectHTMLAttributes<HTMLSelectElement> & { options: { value: string; label: string }[] }) {
  const id = useId();
  return (
    <div className="ag-field">
      <label className="ag-field__label" htmlFor={id}>{label}</label>
      <select id={id} className="ag-field__input" aria-invalid={error ? true : undefined}
        aria-describedby={error || hint ? `${id}-d` : undefined} {...select}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
      {(error || hint) && <span id={`${id}-d`} className={error ? "ag-field__error" : "ag-field__hint"}>{error ?? hint}</span>}
    </div>
  );
}
