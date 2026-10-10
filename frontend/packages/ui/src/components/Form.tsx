import { Select, TextInput } from "@mantine/core";
import type { InputHTMLAttributes, ReactNode } from "react";
import contained from "./Contained.module.css";

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

/** `contained` puts the label inside the box (`.ag-field--contained` in a miniapp). */
type FieldProps = { label: string; hint?: string; error?: string | null; contained?: boolean };

// The hint goes under the input, as `.ag-field__hint` does in a miniapp.
const ORDER = ["label", "input", "description", "error"] as ("label" | "input" | "description" | "error")[];

export function TextField({ label, hint, error, contained: inside, ...input }: FieldProps & Omit<InputHTMLAttributes<HTMLInputElement>, "size">) {
  return <TextInput label={label} description={hint} error={error} inputWrapperOrder={ORDER} classNames={inside ? contained : undefined} {...input} />;
}

// A Mantine combobox, so the open list has the theme's look (a native list is drawn by the OS).
export function SelectField({ label, hint, error, contained: inside, options, value, onChange, name, disabled }: FieldProps & {
  options: { value: string; label: string }[]; value?: string; onChange?: (value: string) => void;
  name?: string; disabled?: boolean;
}) {
  return (
    <Select label={label} description={hint} error={error} data={options} inputWrapperOrder={ORDER} name={name} classNames={inside ? contained : undefined}
      disabled={disabled} value={value} onChange={(v) => v !== null && onChange?.(v)} allowDeselect={false} />
  );
}
