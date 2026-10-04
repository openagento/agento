import { apiFetch, useQuery } from "@agento/api";
import { EmptyState, ErrorState, LoadingState, PageHeader } from "@agento/ui";
import { EnablementGroup, type EnablementItem } from "./Enablement";
import { ScopePicker } from "./ScopePicker";
import { AdminOnly, message, useConfigWrite, useScope } from "./shared";

type Toolsets = { toolset: string; tools: EnablementItem[] }[];

function ToolList() {
  const { ready, query } = useScope();
  const tools = useQuery({
    queryKey: ["admin-tools", query], enabled: ready,
    queryFn: ({ signal }) => apiFetch<Toolsets>(`/api/admin/tools?${query}`, { signal }),
  });
  const write = useConfigWrite("admin-tools");
  if (!ready) return <EmptyState title="Choose a scope" />;
  if (tools.isPending) return <LoadingState />;
  if (tools.error) return <ErrorState message={message(tools.error)} onRetry={() => void tools.refetch()} />;
  if (!tools.data.length) return <EmptyState title="No tools">No enabled module declares a tool.</EmptyState>;
  return <>{tools.data.map((t) => <EnablementGroup key={t.toolset} title={t.toolset} items={t.tools} write={write} />)}</>;
}

export function Tools() {
  return (
    <AdminOnly>
      <div className="ag-stack">
        <PageHeader title="Tools" description="Which tools an agent may call. A tool is off unless it is enabled." />
        <ScopePicker />
        <ToolList />
      </div>
    </AdminOnly>
  );
}
