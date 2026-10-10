import { useSession } from "@agento/api";
import { PageHeader } from "@agento/ui";
import { Dashboard } from "./admin/Dashboard";

export function Home() {
  const user = useSession();
  if (user?.role === "admin") return <Dashboard />;
  return <PageHeader title={`Hello, ${user?.username ?? ""}`} description="Use the navigation to open a screen." />;
}
