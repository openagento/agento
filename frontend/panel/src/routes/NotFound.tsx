import { EmptyState, useDocumentTitle, useT } from "@agento/ui";

export function NotFound() {
  const t = useT("panel");
  useDocumentTitle(t("not_found.title"));
  return <EmptyState title={t("not_found.title")}>{t("not_found.text")}</EmptyState>;
}
