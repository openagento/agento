import { apiFetch, useQuery } from "@agento/api";
import { EmptyState, ErrorState, LoadingState, PageHeader } from "@agento/ui";
import { EnablementGroup, type EnablementItem } from "./Enablement";
import { ScopePicker } from "./ScopePicker";
import { AdminOnly, message, useConfigWrite, useScope } from "./shared";

function SkillList() {
  const { ready, query } = useScope();
  const skills = useQuery({
    queryKey: ["admin-skills", query], enabled: ready,
    queryFn: ({ signal }) => apiFetch<EnablementItem[]>(`/api/admin/skills?${query}`, { signal }),
  });
  const write = useConfigWrite("admin-skills");
  if (!ready) return <EmptyState title="Choose a scope" />;
  if (skills.isPending) return <LoadingState />;
  if (skills.error) return <ErrorState message={message(skills.error)} onRetry={() => void skills.refetch()} />;
  if (!skills.data.length) return <EmptyState title="No skills" />;
  return <EnablementGroup title="Skills" items={skills.data} write={write} />;
}

export function Skills() {
  return (
    <AdminOnly>
      <div className="ag-stack">
        <PageHeader title="Skills" description="Which skills an agent gets. A skill is off unless it is enabled." />
        <ScopePicker />
        <SkillList />
      </div>
    </AdminOnly>
  );
}
