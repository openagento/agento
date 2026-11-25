import { describe, expect, it } from "vitest";
import {
  changeCount, initialChecked, lockedLeaves, nodeState, toggle, toPayload, toTreeData, type RoleResources, type TreeTexts,
} from "./roleTree";

const TEXTS: TreeTexts = { admin: "Admin", operations: "Operations", tools: "Tools", title: (_namespace, _key, serverTitle) => serverTitle };

const RES: RoleResources = {
  operations: [
    { id: "artifact.launch", title: "Launch a miniapp", granted: true, inherited: false, builtin: false },
    { id: "conversation.run_details", title: "See run details", granted: false, inherited: false, builtin: true },
    { id: "app.report", title: "Read reports", granted: true, inherited: true, builtin: false },
  ],
  toolsets: [
    { toolset: "jira", tools: [
      { name: "jira_search", enabled: true, granted: false, inherited: true },
      { name: "jira_comment", enabled: true, granted: true, inherited: true },
      { name: "jira_delete", enabled: false, granted: false, inherited: false },
    ] },
    { toolset: "versioned_artifacts", tools: [{ name: "versioned_artifact_list", enabled: true, granted: false, inherited: false }] },
  ],
  admin: [],
};
const leaf = (id: string, title: string, granted = false, builtin = false) => ({ id, title, granted, builtin });
const GLOBAL: RoleResources = { operations: [], toolsets: [], admin: [
  { screen: "Jobs", leaves: [leaf("admin.jobs", "View", true)] },
  { screen: "Users", leaves: [leaf("admin.users", "View"), leaf("admin.users.edit", "Edit", true)] },
] };

describe("roleTree", () => {
  it("builds groups with leaf values and drops empty groups on search", () => {
    const full = toTreeData(RES, "", TEXTS);
    expect(full.map((n) => n.value)).toEqual(["grp:ops", "grp:tools"]);
    expect(full[1].children?.map((n) => n.value)).toEqual(["grp:ts:jira", "grp:ts:versioned_artifacts"]);
    const found = toTreeData(RES, "DELETE", TEXTS);
    expect(found.map((n) => n.value)).toEqual(["grp:tools"]);
    expect(found[0].children?.[0].children?.map((n) => n.value)).toEqual(["tool:jira_delete"]);
    expect(toTreeData(RES, "jira", TEXTS)[0].children?.[0].children).toHaveLength(3); // the toolset name matches
    expect(toTreeData(RES, "run_details", TEXTS)[0].children?.map((n) => n.value)).toEqual(["op:conversation.run_details"]);
  });

  it("locks every inherited leaf (with or without a row here) and built-in leaves; they start checked", () => {
    expect([...lockedLeaves(RES)].sort())
      .toEqual(["op:app.report", "op:conversation.run_details", "tool:jira_comment", "tool:jira_search"]);
    expect([...initialChecked(RES)].sort()).toEqual([
      "op:app.report", "op:artifact.launch", "op:conversation.run_details", "tool:jira_comment", "tool:jira_search",
    ]);
  });

  it("node state follows its leaves", () => {
    const [ops, tools] = toTreeData(RES, "", TEXTS);
    const checked = initialChecked(RES);
    expect(nodeState(ops, checked)).toBe("checked");
    expect(nodeState(tools, checked)).toBe("indeterminate");
    expect(nodeState(tools.children![1], checked)).toBe("none");
  });

  it("toggle skips locked leaves and touches only the given (visible) leaves", () => {
    const locked = lockedLeaves(RES);
    const [, tools] = toTreeData(RES, "", TEXTS);
    const cleared = toggle(initialChecked(RES), tools, false, locked);
    expect([...cleared].sort()).toEqual([
      "op:app.report", "op:artifact.launch", "op:conversation.run_details", "tool:jira_comment", "tool:jira_search",
    ]);
    const [visible] = toTreeData(RES, "versioned_artifact_list", TEXTS);
    const one = toggle(cleared, visible, true, locked);
    expect(one.has("tool:versioned_artifact_list")).toBe(true);
    expect(one.has("tool:jira_delete")).toBe(false);
  });

  it("the payload leaves out locked leaves with no row here and keeps a redundant row", () => {
    const checked = initialChecked(RES);
    expect(toPayload(checked, RES)).toEqual({ tools: ["jira_comment"], operations: ["artifact.launch", "app.report"] });
    // Clear cannot drop an inherited row: it stays checked and sent.
    const [ops, tools] = toTreeData(RES, "", TEXTS);
    const cleared = [ops, tools].reduce((s, n) => toggle(s, n, false, lockedLeaves(RES)), checked);
    expect(toPayload(cleared, RES)).toEqual({ tools: ["jira_comment"], operations: ["app.report"] });
    expect(changeCount(checked, initialChecked(RES))).toBe(0);
    expect(changeCount(new Set([...checked, "tool:jira_delete"]), checked)).toBe(1);
  });

  it("builds the admin group per screen and sends the checked leaves as the global payload", () => {
    const [admin] = toTreeData(GLOBAL, "", TEXTS);
    expect(admin.value).toBe("grp:admin");
    expect(admin.children?.map((n) => [n.value, n.children?.map((l) => l.value)])).toEqual([
      ["grp:admin:Jobs", ["adm:admin.jobs"]], ["grp:admin:Users", ["adm:admin.users", "adm:admin.users.edit"]],
    ]);
    expect(toTreeData(GLOBAL, "users", TEXTS)[0].children?.map((n) => n.value)).toEqual(["grp:admin:Users"]);
    const checked = initialChecked(GLOBAL);
    expect([...checked].sort()).toEqual(["adm:admin.jobs", "adm:admin.users.edit"]);
    expect(toPayload(new Set([...checked, "adm:admin.users"]), GLOBAL))
      .toEqual({ admin: ["admin.jobs", "admin.users", "admin.users.edit"] });
  });

  it("locks every built-in admin leaf and never sends one without a row", () => {
    const res: RoleResources = { ...GLOBAL, admin: [{ screen: "Jobs", leaves: [leaf("admin.jobs", "View", false, true)] }] };
    expect([...lockedLeaves(res)]).toEqual(["adm:admin.jobs"]);
    expect(toPayload(initialChecked(res), res)).toEqual({ admin: [] });
  });

  it("shows a title from its catalog key, searches the shown text and keeps the node values", () => {
    const titles: Record<string, string> = { "panel:acl.screen.admin.users": "Użytkownicy", "panel:acl.admin.users.edit": "Edycja",
      "miniapps:acl.artifact.launch": "Uruchamianie miniaplikacji" };
    const polish: TreeTexts = {
      admin: "Administracja", operations: "Operacje", tools: "Narzędzia",
      title: (namespace, key, serverTitle) => titles[`${namespace}:${key}`] ?? serverTitle,
    };
    const global: RoleResources = { ...GLOBAL, admin: GLOBAL.admin.map((screen) => ({ ...screen, namespace: "panel" })) };
    const [admin] = toTreeData(global, "", polish);
    expect([admin.label, ...admin.children!.map((n) => n.label)]).toEqual(["Administracja", "Jobs", "Użytkownicy"]);
    expect(admin.children![1].value).toBe("grp:admin:Users");
    expect(admin.children![1].children!.map((n) => n.label)).toEqual(["View", "Edycja"]);
    expect(toTreeData(global, "edyc", polish)[0].children![0].children!.map((n) => n.value)).toEqual(["adm:admin.users.edit"]);
    const res: RoleResources = { ...RES, operations: RES.operations.map((o) => ({ ...o, namespace: o.id.split(".")[0] === "artifact" ? "miniapps" : "x" })) };
    const [operations] = toTreeData(res, "uruchamianie", polish);
    expect([operations.label, ...operations.children!.map((n) => [n.value, n.label])]).toEqual(["Operacje", ["op:artifact.launch", "Uruchamianie miniaplikacji"]]);
  });
});
