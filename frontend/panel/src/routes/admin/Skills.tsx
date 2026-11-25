import { useCallback } from "react";
import { PageHeader, useT } from "@agento/ui";
import type { EnablementItem, Groups } from "../enablementTree";
import { EnablementTree } from "./EnablementTree";
import { ScopePicker } from "./ScopePicker";

export function Skills() {
  const t = useT("panel");
  const title = t("skills.title");
  const groupsOf = useCallback((data: EnablementItem[]): Groups =>
    data.length ? [{ group: "skills", label: title, items: data }] : [], [title]);
  return (
    <div className="ag-stack">
      <PageHeader title={title} description={t("skills.description")} />
      <ScopePicker />
      <EnablementTree listKey="admin-skills" url="/api/admin/skills" groupsOf={groupsOf}
        emptyTitle={t("skills.empty.title")} emptyText={t("skills.empty.text")} />
    </div>
  );
}
