import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Grants } from "./Grants";
import { renderAt, stubApi, teardown } from "./admin/testing";

const SCOPES = { workspaces: [{ id: 2, code: "ws", label: "WS", is_active: true }],
  agent_views: [{ id: 3, code: "av", label: "AV", workspace_id: 2, is_active: true }] };
const tool = (name: string, enabled: boolean) => ({ name, path: `tools/${name}/is_enabled`, enabled, explicit_here: false, blocked_by: null });
const TOOLS = [{ toolset: "jira", tools: [tool("jira_search", true), tool("jira_comment", true), tool("jira_delete", false)] }];
const grant = (over: object) => ({ id: 1, role: "user", grant_kind: "tool", name: "jira_search", workspace_id: null,
  agent_view_id: 3, created_at: null, ...over });
const base = {
  "/api/admin/scopes": SCOPES, "/api/admin/tools": TOOLS,
  "/api/admin/grants/options": { roles: ["admin", "user"], operations: ["artifact.launch"] },
  "/api/admin/grants": [] as object[],
};
afterEach(teardown);

/** Opens the sheet and picks the agent view "av — AV". */
async function openSheetAtView() {
  fireEvent.click(await screen.findByRole("button", { name: "Add access" }));
  const sheet = await screen.findByRole("dialog", { name: "Give access" });
  fireEvent.click(within(sheet).getByRole("radio", { name: "Agent view" }));
  fireEvent.click(within(sheet).getByRole("combobox", { name: "Agent view" }));
  fireEvent.click(await screen.findByRole("option", { name: "av — AV" }));
  return sheet;
}
const openTools = (sheet: HTMLElement) => fireEvent.click(within(sheet).getByRole("combobox", { name: "Tools" }));

describe("Grants", () => {
  it("names the place of a grant; the raw id only when it is unknown", async () => {
    await stubApi({ ...base, "/api/admin/grants": [grant({}), grant({ id: 2, name: "jira_comment", agent_view_id: null, workspace_id: 9 })] });
    renderAt("/users", <Grants />);
    expect(await screen.findByText("Agent view · av — AV")).toBeInTheDocument();
    expect(screen.getByText("Workspace · 9")).toBeInTheDocument();
  });

  it("shows an empty state with the same button", async () => {
    await stubApi(base);
    renderAt("/users", <Grants />);
    expect(await screen.findByText("No one has access yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add access" })).toBeInTheDocument();
  });

  it("cannot submit without a place", async () => {
    await stubApi(base);
    renderAt("/users", <Grants />);
    fireEvent.click(await screen.findByRole("button", { name: "Add access" }));
    const sheet = await screen.findByRole("dialog", { name: "Give access" });
    expect(within(sheet).getByRole("button", { name: /^Give access to/ })).toBeDisabled();
  });

  it("two picked tools send two POSTs with agent_view_id, in order", async () => {
    const api = await stubApi({ ...base, "POST /api/admin/grants": { id: 9 } });
    renderAt("/users", <Grants />);
    const sheet = await openSheetAtView();
    openTools(sheet);
    fireEvent.click(await screen.findByRole("option", { name: "jira_search" }));
    fireEvent.click(await screen.findByRole("option", { name: "jira_comment" }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Give access to 2" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/grants")).toHaveLength(2));
    expect(api.calls("POST", "/api/admin/grants").map((c) => c.body)).toEqual([
      { role: "user", kind: "tool", name: "jira_search", agent_view_id: 3 },
      { role: "user", kind: "tool", name: "jira_comment", agent_view_id: 3 },
    ]);
    expect(api.calls("GET", "/api/admin/tools")[0].url.search).toBe("?scope=agent_view&scope_id=3");
  });

  it("marks a tool that is off there and leaves out what the role already has there", async () => {
    await stubApi({ ...base, "/api/admin/grants": [grant({})] });
    renderAt("/users", <Grants />);
    const sheet = await openSheetAtView();
    openTools(sheet);
    expect(await screen.findByRole("option", { name: "jira_delete (off here)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "jira_comment" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "jira_search" })).toBeNull();
    expect(within(sheet).getByText(/A grant does not enable a tool/)).toBeInTheDocument();
  });

  it("a refusal stops the batch and names the item", async () => {
    const api = await stubApi({
      ...base,
      "POST /api/admin/grants": (b: { name: string }) => b.name === "jira_search"
        ? new Response(JSON.stringify({ error: "Refused" }), { status: 400 }) : { id: 9 },
    });
    renderAt("/users", <Grants />);
    const sheet = await openSheetAtView();
    openTools(sheet);
    fireEvent.click(await screen.findByRole("option", { name: "jira_search" }));
    fireEvent.click(await screen.findByRole("option", { name: "jira_comment" }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Give access to 2" }));
    expect(await within(sheet).findByText(/jira_search: Refused/)).toBeInTheDocument();
    expect(api.calls("POST", "/api/admin/grants")).toHaveLength(1);
  });

  it("Remove asks first and says what is lost", async () => {
    const api = await stubApi({ ...base, "/api/admin/grants": [grant({ grant_kind: "operation", name: "artifact.launch" })],
      "DELETE /api/admin/grants/1": null });
    renderAt("/users", <Grants />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    const confirm = await screen.findByRole("dialog", { name: "Remove this access?" });
    expect(within(confirm).getByText(/can no longer use artifact\.launch in Agent view · av — AV\. Open launches there end\./))
      .toBeInTheDocument();
    expect(api.calls("DELETE", "/api/admin/grants/1")).toEqual([]);
    fireEvent.click(within(confirm).getByRole("button", { name: "Remove" }));
    await vi.waitFor(() => expect(api.calls("DELETE", "/api/admin/grants/1")).toHaveLength(1));
  });
});
