import { useCan, useSession } from "@agento/api";
import { PageHeader } from "@agento/ui";
import { Dashboard } from "./admin/Dashboard";

export function Home() {
  const user = useSession();
  const dashboard = useCan("admin.dashboard");
  if (dashboard) return <Dashboard />;
  return <PageHeader title={`Hello, ${user?.username ?? ""}`} description="Use the navigation to open a screen." />;
}
