import { useCallback } from "react";
import { useSearchParams } from "react-router";
import { Tabs } from "@mantine/core";
import { useSession } from "@agento/api";
import { PageHeader } from "@agento/ui";
import type { EnablementItem, Groups } from "../enablementTree";
import { EnablementTree } from "./EnablementTree";
import { ScopePicker } from "./ScopePicker";
import { ToolCatalog } from "./ToolCatalog";
import { ToolLabels } from "./ToolLabels";

export type Toolsets = { toolset: string; tools: EnablementItem[] }[];

const ADMIN_TABS = ["catalog", "labels"];

/** Enablement for every reader; Catalog and Labels for the admin role only (PRD §4). The tab is in
 *  `?tab=`, beside the scope. The Enablement panel stays mounted, so its draft survives a tab switch. */
export function Tools() {
  const admin = useSession()?.role === "admin";
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab") ?? "";
  const tab = admin && ADMIN_TABS.includes(asked) ? asked : "enablement";
  const setTab = (t: string | null) => setParams((p) => {
    const next = new URLSearchParams(p);
    if (t && t !== "enablement") next.set("tab", t);
    else next.delete("tab");
    return next;
  });
  const groupsOf = useCallback((data: Toolsets): Groups =>
    data.map((t) => ({ group: t.toolset, items: t.tools })), []);
  const enablement = (
    <div className="ag-stack">
      <ScopePicker />
      <EnablementTree listKey="admin-tools" url="/api/admin/tools" groupsOf={groupsOf} toolRisk
        emptyTitle="No tools" emptyText="No enabled module declares a tool." />
    </div>
  );
  return (
    <div className="ag-stack">
      <PageHeader title="Tools" description="Which tools an agent may call. A tool is off unless it is enabled." />
      {admin ? (
        <Tabs value={tab} onChange={setTab}>
          <Tabs.List>
            <Tabs.Tab value="enablement">Enablement</Tabs.Tab>
            <Tabs.Tab value="catalog">Catalog</Tabs.Tab>
            <Tabs.Tab value="labels">Labels</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="enablement" pt="md">{enablement}</Tabs.Panel>
          {/* Mounted only while shown: each polls the catalog. */}
          <Tabs.Panel value="catalog" pt="md">{tab === "catalog" && <ToolCatalog />}</Tabs.Panel>
          <Tabs.Panel value="labels" pt="md">{tab === "labels" && <ToolLabels />}</Tabs.Panel>
        </Tabs>
      ) : enablement}
    </div>
  );
}
