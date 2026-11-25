import { EmptyState, useDocumentTitle, useT } from "@agento/ui";

export function NotAvailable() {
  const t = useT("panel");
  useDocumentTitle(t("not_available.title"));
  return <EmptyState title={t("not_available.title")}>{t("not_available.text")}</EmptyState>;
}
