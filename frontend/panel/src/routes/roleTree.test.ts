import { describe, expect, it } from "vitest";
import {
  changeCount, initialChecked, lockedLeaves, nodeState, toggle, toPayload, toTreeData, type RoleResources,
} from "./roleTree";

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
    { toolset: "miniapps", tools: [{ name: "miniapp_list", enabled: true, granted: false, inherited: false }] },
  ],
};

describe("roleTree", () => {
  it("builds groups with leaf values and drops empty groups on search", () => {
    const full = toTreeData(RES, "");
    expect(full.map((n) => n.value)).toEqual(["grp:ops", "grp:tools"]);
    expect(full[1].children?.map((n) => n.value)).toEqual(["grp:ts:jira", "grp:ts:miniapps"]);
    const found = toTreeData(RES, "DELETE");
    expect(found.map((n) => n.value)).toEqual(["grp:tools"]);
    expect(found[0].children?.[0].children?.map((n) => n.value)).toEqual(["tool:jira_delete"]);
    expect(toTreeData(RES, "jira")[0].children?.[0].children).toHaveLength(3); // the toolset name matches
    expect(toTreeData(RES, "run_details")[0].children?.map((n) => n.value)).toEqual(["op:conversation.run_details"]);
  });

  it("locks every inherited leaf (with or without a row here) and built-in leaves; they start checked", () => {
    expect([...lockedLeaves(RES)].sort())
      .toEqual(["op:app.report", "op:conversation.run_details", "tool:jira_comment", "tool:jira_search"]);
    expect([...initialChecked(RES)].sort()).toEqual([
      "op:app.report", "op:artifact.launch", "op:conversation.run_details", "tool:jira_comment", "tool:jira_search",
    ]);
  });

  it("node state follows its leaves", () => {
    const [ops, tools] = toTreeData(RES, "");
    const checked = initialChecked(RES);
    expect(nodeState(ops, checked)).toBe("checked");
    expect(nodeState(tools, checked)).toBe("indeterminate");
    expect(nodeState(tools.children![1], checked)).toBe("none");
  });

  it("toggle skips locked leaves and touches only the given (visible) leaves", () => {
    const locked = lockedLeaves(RES);
    const [, tools] = toTreeData(RES, "");
    const cleared = toggle(initialChecked(RES), tools, false, locked);
    expect([...cleared].sort()).toEqual([
      "op:app.report", "op:artifact.launch", "op:conversation.run_details", "tool:jira_comment", "tool:jira_search",
    ]);
    const [visible] = toTreeData(RES, "miniapp_list");
    const one = toggle(cleared, visible, true, locked);
    expect(one.has("tool:miniapp_list")).toBe(true);
    expect(one.has("tool:jira_delete")).toBe(false);
  });

  it("the payload leaves out locked leaves with no row here and keeps a redundant row", () => {
    const checked = initialChecked(RES);
    expect(toPayload(checked, RES)).toEqual({ tools: ["jira_comment"], operations: ["artifact.launch", "app.report"] });
    // Clear cannot drop an inherited row: it stays checked and sent.
    const [ops, tools] = toTreeData(RES, "");
    const cleared = [ops, tools].reduce((s, n) => toggle(s, n, false, lockedLeaves(RES)), checked);
    expect(toPayload(cleared, RES)).toEqual({ tools: ["jira_comment"], operations: ["app.report"] });
    expect(changeCount(checked, initialChecked(RES))).toBe(0);
    expect(changeCount(new Set([...checked, "tool:jira_delete"]), checked)).toBe(1);
  });
});
