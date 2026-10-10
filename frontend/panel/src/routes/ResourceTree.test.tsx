// The expand/collapse buttons appear only when they would do something, over the VISIBLE data.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentoUiProvider } from "@agento/ui";
import { ResourceTree } from "./ResourceTree";
import { toTreeData, type Groups } from "./enablementTree";

const item = (name: string) => ({ name, path: `tools/${name}/is_enabled`, enabled: false, explicit_here: false });
const GROUPS: Groups = [
  { group: "jira", items: [item("jira_search"), item("jira_get")] },
  { group: "git", items: [item("git_log")] },
];
const NESTED = [{
  value: "grp:tools", label: "Tools",
  children: [{ value: "grp:ts:jira", label: "jira", children: [{ value: "tools/jira_search", label: "jira_search" }] }],
}];

const at = (toData: (s: string) => ReturnType<typeof toTreeData>) => render(
  <AgentoUiProvider env="test">
    <ResourceTree toData={toData} checked={new Set()} locked={new Set()} onChange={() => undefined} busy={false}
      renderInfo={() => null} emptyTitle="Nothing" emptyText="Nothing at all" />
  </AgentoUiProvider>,
);
const expand = () => screen.queryByRole("button", { name: "Expand all" });
const collapse = () => screen.queryByRole("button", { name: "Collapse all" });

describe("ResourceTree expand/collapse", () => {
  it("starts fully expanded, so only Collapse all is offered", () => {
    at((s) => toTreeData(GROUPS, s));
    expect(expand()).not.toBeInTheDocument();
    expect(collapse()).toBeInTheDocument();
  });

  it("fully collapsed offers only Expand all, and expanding again mirrors it", () => {
    at((s) => toTreeData(GROUPS, s));
    fireEvent.click(collapse()!);
    expect(collapse()).not.toBeInTheDocument();
    expect(expand()).toBeInTheDocument();
    fireEvent.click(expand()!);
    expect(expand()).not.toBeInTheDocument();
    expect(collapse()).toBeInTheDocument();
  });

  it("one group closed while another is open offers both", () => {
    at((s) => toTreeData(GROUPS, s));
    fireEvent.click(screen.getByRole("button", { name: "Collapse git" }));
    expect(expand()).toBeInTheDocument();
    expect(collapse()).toBeInTheDocument();
  });

  it("counts groups at every depth: a nested group closed under an open parent offers both", () => {
    at(() => NESTED);
    fireEvent.click(screen.getByRole("button", { name: "Collapse jira" }));
    expect(screen.getByRole("button", { name: "Collapse Tools" })).toBeInTheDocument();
    expect(expand()).toBeInTheDocument();
    expect(collapse()).toBeInTheDocument();
  });

  it("follows the visible data, not the whole tree: a search that leaves one open group hides Expand all", () => {
    at((s) => toTreeData(GROUPS, s));
    fireEvent.click(screen.getByRole("button", { name: "Collapse git" }));
    expect(expand()).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Search resources" }), { target: { value: "jira_search" } });
    expect(expand()).not.toBeInTheDocument();
    expect(collapse()).toBeInTheDocument();
  });

  it("a search that matches nothing hides both", () => {
    at((s) => toTreeData(GROUPS, s));
    fireEvent.change(screen.getByRole("textbox", { name: "Search resources" }), { target: { value: "zzz" } });
    expect(expand()).not.toBeInTheDocument();
    expect(collapse()).not.toBeInTheDocument();
    expect(screen.getByText("Nothing matches")).toBeInTheDocument();
  });
});
