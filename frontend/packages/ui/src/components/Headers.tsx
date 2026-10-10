import type { ReactNode } from "react";

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
