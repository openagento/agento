import { useSession } from "@agento/api";
import { PageHeader } from "@agento/ui";

export function Home() {
  const user = useSession();
  return <PageHeader title={`Hello, ${user?.username ?? ""}`} description="Use the navigation to open a screen." />;
}
