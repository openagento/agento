import { fireEvent, screen } from "@testing-library/react";
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

const save = () => fireEvent.click(screen.getByRole("button", { name: "Save" }));

// The save contract itself (drafts, batching, failures, scope binding) lives in
// enablementScreens.test.tsx, which runs it against a server double that remembers writes.
describe("Tools", () => {
  it("reads the scope from the URL, so a reload keeps it", async () => {
    const api = await stubApi({ "/api/admin/scopes": SCOPES, "/api/admin/tools": TOOLS });
    renderAt("/admin/tools?scope=agent_view&scope_id=3", <Tools />);
    expect(await screen.findByText("jira_search")).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/tools")[0].url.search).toBe("?scope=agent_view&scope_id=3");
    expect(await screen.findByDisplayValue("av — AV")).toBeInTheDocument();
  });

  it("a blocked tool cannot be toggled; only an inherited enable is marked (TUI prompt_label)", async () => {
    await stubApi({ "/api/admin/scopes": SCOPES, "/api/admin/tools": TOOLS });
    renderAt("/admin/tools", <Tools />);
    expect(await screen.findByRole("checkbox", { name: "jira_delete" })).toBeDisabled();
    expect(screen.getAllByText("inherited")).toHaveLength(1);
    expect(screen.getByRole("checkbox", { name: "jira_get" }).closest(".mantine-Group-root")).toHaveTextContent("inherited");
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
});
