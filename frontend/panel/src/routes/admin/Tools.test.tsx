import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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

describe("Tools", () => {
  it("reads the scope from the URL, so a reload keeps it", async () => {
    const api = await stubApi({ "/api/admin/scopes": SCOPES, "/api/admin/tools": TOOLS });
    renderAt("/admin/tools?scope=agent_view&scope_id=3", <Tools />);
    expect(await screen.findByText("jira_search")).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/tools")[0].url.search).toBe("?scope=agent_view&scope_id=3");
    expect(await screen.findByDisplayValue("av — AV")).toBeInTheDocument();
  });

  it("a toggle PUTs one tool gate at the scope; Enable all skips blocked and already-on tools", async () => {
    const api = await stubApi({
      "/api/admin/scopes": SCOPES, "/api/admin/tools": TOOLS,
      "PUT /api/admin/config": (b: { path: string }) => ({ path: b.path, reset: [] }),
    });
    renderAt("/admin/tools?scope=workspace&scope_id=2", <Tools />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "jira_search" }));
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/config")).toHaveLength(1));
    expect(api.calls("PUT", "/api/admin/config")[0].body)
      .toEqual({ path: "tools/jira_search/is_enabled", value: "1", scope: "workspace", scope_id: 2 });

    fireEvent.click(await screen.findByRole("button", { name: "Enable all" }));
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/config")).toHaveLength(2));
    expect(api.calls("PUT", "/api/admin/config")[1].body.path).toBe("tools/jira_search/is_enabled");
  });

  it("a blocked tool cannot be toggled; only an inherited enable is marked (TUI prompt_label)", async () => {
    await stubApi({ "/api/admin/scopes": SCOPES, "/api/admin/tools": TOOLS });
    renderAt("/admin/tools", <Tools />);
    expect(await screen.findByRole("checkbox", { name: "jira_delete" })).toBeDisabled();
    expect(screen.getAllByText("inherited")).toHaveLength(1);
    expect(screen.getByRole("checkbox", { name: "jira_get" }).closest(".mantine-Group-root")).toHaveTextContent("inherited");
  });

  it("Enable all waits for every write, even after one fails, then reports it and refreshes", async () => {
    let finishSlow!: () => void;
    const slow = new Promise<void>((r) => { finishSlow = r; });
    const api = await stubApi({
      "/api/admin/scopes": SCOPES,
      "/api/admin/tools": [{ toolset: "jira", tools: [tool("jira_search", false, false), tool("jira_get", false, false)] }],
      "PUT /api/admin/config": (b: { path: string }) => b.path === "tools/jira_search/is_enabled"
        ? new Response(JSON.stringify({ error: "Refused" }), { status: 400 })
        : slow.then(() => ({ path: b.path, reset: [] })),
    });
    renderAt("/admin/tools", <Tools />);
    fireEvent.click(await screen.findByRole("button", { name: "Enable all" }));
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/config")).toHaveLength(2));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByRole("checkbox", { name: "jira_get" })).toBeDisabled();
    expect(api.calls("GET", "/api/admin/tools")).toHaveLength(1);

    finishSlow();
    expect(await screen.findByText(/Saved 1 of 2\. tools\/jira_search\/is_enabled: Refused/)).toBeInTheDocument();
    await vi.waitFor(() => expect(api.calls("GET", "/api/admin/tools")).toHaveLength(2));
    await vi.waitFor(() => expect(screen.getByRole("checkbox", { name: "jira_get" })).toBeEnabled());
  });

  it("shows the reset list of a PUT as a notification", async () => {
    await stubApi({
      "/api/admin/scopes": SCOPES, "/api/admin/tools": TOOLS,
      "PUT /api/admin/config": { path: "tools/jira_search/is_enabled", reset: ["jira/other"] },
    });
    renderAt("/admin/tools", <Tools />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "jira_search" }));
    expect(await screen.findByText("Also reset: jira/other")).toBeInTheDocument();
  });
});
