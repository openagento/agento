import { useCallback } from "react";
import { PageHeader } from "@agento/ui";
import type { EnablementItem, Groups } from "../enablementTree";
import { EnablementTree } from "./EnablementTree";
import { ScopePicker } from "./ScopePicker";
import { AdminOnly } from "./shared";

export function Skills() {
  const groupsOf = useCallback((data: EnablementItem[]): Groups =>
    data.length ? [{ group: "Skills", items: data }] : [], []);
  return (
    <AdminOnly>
      <div className="ag-stack">
        <PageHeader title="Skills" description="Which skills an agent gets. A skill is off unless it is enabled." />
        <ScopePicker />
        <EnablementTree listKey="admin-skills" url="/api/admin/skills" groupsOf={groupsOf}
          emptyTitle="No skills" emptyText="No enabled module declares a skill." />
      </div>
    </AdminOnly>
  );
}
