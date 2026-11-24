import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Tools } from "./Tools";
import { renderAt, stubApi, teardown } from "./testing";

const SCOPES = { workspaces: [{ id: 2, code: "ws", label: "WS", is_active: true }],
  agent_views: [{ id: 3, code: "av", label: "AV", workspace_id: 2, is_active: true }] };
const tool = (name: string, enabled: boolean, explicit_here: boolean, blocked_by: string | null = null) =>
  ({ name, path: `tools/${name}/is_enabled`, enabled, explicit_here, blocked_by });
const TOOLS = [{ toolset: "jira", tools: [
  tool("jira_search", false, false), tool("jira_comment", true, true), tool("jira_get", true, false),
  tool("jira_delete", true, false, "jira"),
] }];

afterEach(teardown);

const LABELS = [
  { id: 1, key: "private-data", name: "Private data access", protected: true, usage: 1 },
  { id: 2, key: "untrusted-content", name: "Untrusted content", protected: true, usage: 1 },
  { id: 3, key: "external-communication", name: "External communication", protected: true, usage: 1 },
  { id: 4, key: "stub-in-test", name: "Stub in test", protected: true, usage: 0 },
];
const labelled = (name: string, enabled: boolean, labels: string[], requires: string | null = null) =>
  ({ ...tool(name, enabled, true), requires, catalog: { id: name.length, source: "module:m", labels, reviewed: true, revision: 1 } });
const RISKY = [
  { toolset: "mail", tools: [labelled("mail_read", true, ["private-data"]), labelled("mail_send", false, ["external-communication"], "mail_read")] },
  { toolset: "web", tools: [labelled("web_fetch", true, ["untrusted-content"])] },
];
const ADMIN_TOOLS = { "/api/admin/scopes": SCOPES, "/api/admin/tools": RISKY, "/api/admin/tool-labels": LABELS };
const WARNING = "Lethal trifecta";

const save = () => fireEvent.click(screen.getByRole("button", { name: "Save" }));

// The save contract itself (drafts, batching, failures, scope binding) and the URL scope live in
// enablementScreens.test.tsx, which runs it against a server double that remembers writes.
describe("Tools", () => {
  it("a blocked tool cannot be toggled; only an inherited enable is marked (TUI prompt_label)", async () => {
    await stubApi({ "/api/admin/scopes": SCOPES, "/api/admin/tools": TOOLS });
    renderAt("/admin/tools", <Tools />);
    expect(await screen.findByRole("checkbox", { name: "jira_delete" })).toBeDisabled();
    expect(screen.getAllByText("Inherited")).toHaveLength(1);
    expect(screen.getByRole("checkbox", { name: "jira_get" }).closest("[role=treeitem]")).toHaveTextContent("Inherited");
  });

  it("shows the reset list of a PUT as a notification", async () => {
    await stubApi({
      "/api/admin/scopes": SCOPES, "/api/admin/tools": TOOLS,
      "PUT /api/admin/config": { path: "tools/jira_search/is_enabled", reset: ["jira/other"] },
    });
    renderAt("/admin/tools", <Tools />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "jira_search" }));
    save();
    expect(await screen.findByText("Also reset: jira/other")).toBeInTheDocument();
  });

  it("warning updates on draft toggle without save", async () => {
    const api = await stubApi(ADMIN_TOOLS);
    renderAt("/admin/tools", <Tools />);
    const send = await screen.findByRole("checkbox", { name: "mail_send" });
    expect(screen.queryByText(WARNING)).not.toBeInTheDocument();
    fireEvent.click(send);
    expect(await screen.findByText(WARNING)).toBeInTheDocument();
    expect(within(screen.getByRole("alert")).getByText(/mail_send/)).toBeInTheDocument();
    // The master off in the draft turns its dependant off too.
    fireEvent.click(screen.getByRole("checkbox", { name: "mail_read" }));
    expect(screen.queryByText(WARNING)).not.toBeInTheDocument();
    expect(api.calls("PUT", "/api/admin/config")).toHaveLength(0);
  });

  it("filtered group does not change result", async () => {
    await stubApi(ADMIN_TOOLS);
    renderAt("/admin/tools", <Tools />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "mail_send" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search resources" }), { target: { value: "mail" } });
    expect(screen.queryByRole("checkbox", { name: "web_fetch" })).not.toBeInTheDocument();
    expect(screen.getByText(WARNING)).toBeInTheDocument();
  });

  it("labels shown on leaves", async () => {
    await stubApi(ADMIN_TOOLS);
    renderAt("/admin/tools", <Tools />);
    const leaf = (await screen.findByRole("checkbox", { name: "mail_read" })).closest("[role=treeitem]")!;
    expect(await within(leaf as HTMLElement).findByText("Private data access")).toBeInTheDocument();
    expect(leaf.querySelector("svg[data-risk='private-data']")).not.toBeNull();
  });

  it("no warning and no labels for non-admin", async () => {
    // A server never sends `catalog` to such a reader; the screen must not show it even if it did.
    const api = await stubApi({ ...ADMIN_TOOLS, "/api/admin/tools": RISKY }, ["admin.tools", "admin.config"]);
    renderAt("/admin/tools", <Tools />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "mail_send" }));
    expect(screen.queryByText(WARNING)).not.toBeInTheDocument();
    expect(screen.queryByText("Private data access")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/tool-labels")).toHaveLength(0);
  });

  it("an admin gets the Catalog and Labels tabs; the Enablement draft survives a tab switch", async () => {
    await stubApi(ADMIN_TOOLS);
    renderAt("/admin/tools", <Tools />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "mail_send" }));
    fireEvent.click(screen.getByRole("tab", { name: "Labels" }));
    expect(await screen.findByRole("table", { name: "Tool labels" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Enablement" }));
    expect(screen.getByRole("checkbox", { name: "mail_send" })).toBeChecked();
  });
});
