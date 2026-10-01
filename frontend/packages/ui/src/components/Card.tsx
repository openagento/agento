import type { ReactNode } from "react";

export function Card({ title, actions, meta, children, className }: {
  title?: ReactNode; actions?: ReactNode; meta?: ReactNode; children?: ReactNode; className?: string;
}) {
  return (
    <article className={["ag-card", className].filter(Boolean).join(" ")}>
      {(title || actions) && (
        <header className="ag-card__head">
          {title && <h3 className="ag-card__title">{title}</h3>}
          {actions}
        </header>
      )}
      {children !== undefined && <div className="ag-card__body">{children}</div>}
      {meta && <p className="ag-card__meta">{meta}</p>}
    </article>
  );
}
