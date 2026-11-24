// The expand/collapse buttons appear only when they would do something, over the VISIBLE data.
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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

describe("ResourceTree on a phone", () => {
  afterEach(() => vi.restoreAllMocks());

  it("puts a row's info on its own line under the checkbox, not beside it", () => {
    vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
      matches: query.includes("max-width"), media: query, onchange: null,
      addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false,
    }));
    render(
      <AgentoUiProvider env="test">
        <ResourceTree toData={(s) => toTreeData(GROUPS, s)} checked={new Set()} locked={new Set()} onChange={() => undefined}
          busy={false} renderInfo={(n) => (n.children ? null : <span data-testid="info">{String(n.label)}</span>)}
          emptyTitle="Nothing" emptyText="Nothing at all" />
      </AgentoUiProvider>,
    );
    const box = screen.getByRole("checkbox", { name: "git_log" });
    const info = screen.getAllByTestId("info").find((i) => i.textContent === "git_log")!;
    const row = box.closest(".mantine-Group-root")!;
    expect(row.contains(info)).toBe(false);
    expect(row.parentElement!.contains(info)).toBe(true);
  });
});
