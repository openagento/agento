import { useCallback } from "react";
import { PageHeader } from "@agento/ui";
import type { EnablementItem, Groups } from "../enablementTree";
import { EnablementTree } from "./EnablementTree";
import { ScopePicker } from "./ScopePicker";
import { AdminOnly } from "./shared";

type Toolsets = { toolset: string; tools: EnablementItem[] }[];

export function Tools() {
  const groupsOf = useCallback((data: Toolsets): Groups =>
    data.map((t) => ({ group: t.toolset, items: t.tools })), []);
  return (
    <AdminOnly>
      <div className="ag-stack">
        <PageHeader title="Tools" description="Which tools an agent may call. A tool is off unless it is enabled." />
        <ScopePicker />
        <EnablementTree listKey="admin-tools" url="/api/admin/tools" groupsOf={groupsOf}
          emptyTitle="No tools" emptyText="No enabled module declares a tool." />
      </div>
    </AdminOnly>
  );
}
