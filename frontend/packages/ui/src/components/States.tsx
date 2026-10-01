import type { ReactNode } from "react";
import { Button } from "./Button";

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="ag-state ag-state--empty">
      <p className="ag-state__title">{title}</p>
      {children && <p className="ag-state__text">{children}</p>}
    </div>
  );
}

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="ag-state ag-state--loading" role="status" aria-live="polite">
      <p className="ag-state__title">{label}</p>
    </div>
  );
}

export function ErrorState({ title = "Something went wrong", message, onRetry }: {
  title?: string; message?: string; onRetry?: () => void;
}) {
  return (
    <div className="ag-state ag-state--error" role="alert">
      <p className="ag-state__title">{title}</p>
      {message && <p className="ag-state__text">{message}</p>}
      {onRetry && <p className="ag-state__text"><Button onClick={onRetry}>Try again</Button></p>}
    </div>
  );
}
