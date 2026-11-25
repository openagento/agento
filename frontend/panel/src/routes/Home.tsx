import { useCan, useSession } from "@agento/api";
import { PageHeader, useT } from "@agento/ui";
import { Dashboard } from "./admin/Dashboard";

export function Home() {
  const user = useSession();
  const dashboard = useCan("admin.dashboard");
  const t = useT("panel");
  if (dashboard) return <Dashboard />;
  return <PageHeader title={t("home.greeting", { username: user?.username ?? "" })} description={t("home.hint")} />;
}
