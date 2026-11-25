import type { ReactNode } from "react";
import { Button } from "./Button";
import { useT } from "../i18n";

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="ag-state ag-state--empty">
      <p className="ag-state__title">{title}</p>
      {children && <p className="ag-state__text">{children}</p>}
      {action && <p className="ag-state__text">{action}</p>}
    </div>
  );
}

export function LoadingState({ label }: { label?: string }) {
  const t = useT("ui");
  return (
    <div className="ag-state ag-state--loading" role="status" aria-live="polite">
      <p className="ag-state__title">{label ?? t("state.loading")}</p>
    </div>
  );
}

export function ErrorState({ title, message, onRetry }: {
  title?: string; message?: string; onRetry?: () => void;
}) {
  const t = useT("ui");
  return (
    <div className="ag-state ag-state--error" role="alert">
      <p className="ag-state__title">{title ?? t("state.error")}</p>
      {message && <p className="ag-state__text">{message}</p>}
      {onRetry && <p className="ag-state__text"><Button onClick={onRetry}>{t("state.retry")}</Button></p>}
    </div>
  );
}
