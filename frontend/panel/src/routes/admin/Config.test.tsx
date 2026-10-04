import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Config } from "./Config";
import { renderAt, stubApi, teardown } from "./testing";

const field = (over: object) => ({
  path: "jira/url", label: "URL", description: "", type: "string", source: "db", editable: true,
  allowed_scopes: ["default"], options: null, tester: "", max_length: null, secret: false, is_set: true, value: "https://x", ...over,
});
const FIELDS = [
  field({}),
  // A server bug that sent a secret's value must still render nothing of it.
  field({ path: "jira/token", label: "Token", type: "obscure", secret: true, is_set: true, value: "hunter2", tester: "http" }),
  field({ path: "jira/tools/jira_search/limit", label: "Limit", type: "integer", source: "json", value: "10" }),
];
const base = {
  "/api/admin/scopes": { workspaces: [], agent_views: [{ id: 3, code: "av", label: "AV", workspace_id: 1, is_active: true }] },
  "/api/admin/config/modules": [{ name: "jira", tools: ["jira_search"] }, { name: "smtp", tools: [] }],
  "/api/admin/config": FIELDS,
};
const editOf = (label: string) => {
  const row = screen.getByText(label).closest("tr")!;
  fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
};
afterEach(teardown);

describe("Config", () => {
  it("keeps module, tool and scope in the URL, so a reload keeps them", async () => {
    const api = await stubApi(base);
    renderAt("/admin/config?module=jira&tool=jira_search&scope=agent_view&scope_id=3", <Config />);
    expect(await screen.findByText("Limit")).toBeInTheDocument();
    expect(screen.queryByText("URL")).toBeNull();
    const q = api.calls("GET", "/api/admin/config")[0].url.searchParams;
    expect([q.get("module"), q.get("scope"), q.get("scope_id")]).toEqual(["jira", "agent_view", "3"]);
    expect(screen.queryByText(/ENV overrides/)).toBeNull();
  });

  it("lists a module by its title and keeps its key in the URL", async () => {
    const api = await stubApi({ ...base, "/api/admin/config/modules": [{ name: "jira", tools: [] }, { name: "agent_view", tools: [] }] });
    renderAt("/admin/config", <Config />);
    fireEvent.click(await screen.findByText("Agent View"));
    await vi.waitFor(() => expect(api.calls("GET", "/api/admin/config").map((c) => c.url.searchParams.get("module")))
      .toContain("agent_view"));
    expect(screen.queryByText("agent_view")).toBeNull();
  });

  it("a secret row shows Set, never the value, and its editor is disabled", async () => {
    await stubApi(base);
    renderAt("/admin/config", <Config />);
    expect(await screen.findByText("Token")).toBeInTheDocument();
    expect(screen.queryByText(/hunter2/)).toBeNull();
    expect(screen.getByText("Set")).toBeInTheDocument();
    editOf("Token");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/bin\/agento config:set or the admin TUI/)).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "Value" })).toBeDisabled();
    expect(within(dialog).getByRole("textbox", { name: "Value" })).toHaveValue("");
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.queryByDisplayValue(/hunter2/)).toBeNull();
  });

  it("Save PUTs the value at the scope and names the reset dependents", async () => {
    const api = await stubApi({ ...base, "PUT /api/admin/config": { path: "jira/url", reset: ["jira/project"] } });
    renderAt("/admin/config?scope=agent_view&scope_id=3", <Config />);
    await screen.findByText("URL");
    editOf("URL");
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Value" }), { target: { value: "https://y" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/config")).toHaveLength(1));
    expect(api.calls("PUT", "/api/admin/config")[0].body).toEqual({ path: "jira/url", value: "https://y", scope: "agent_view", scope_id: 3 });
    expect(await screen.findByText("Also reset: jira/project")).toBeInTheDocument();
  });

  it("Remove override asks first, then DELETEs the path at the scope", async () => {
    const api = await stubApi({ ...base, "DELETE /api/admin/config": null });
    renderAt("/admin/config", <Config />);
    await screen.findByText("URL");
    editOf("URL");
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove override" }));
    const confirm = await screen.findByRole("dialog", { name: "Remove this override?" });
    expect(api.calls("DELETE", "/api/admin/config")).toEqual([]);
    fireEvent.click(within(confirm).getByRole("button", { name: "Remove override" }));
    await vi.waitFor(() => expect(api.calls("DELETE", "/api/admin/config")).toHaveLength(1));
    expect(api.calls("DELETE", "/api/admin/config")[0].body).toEqual({ path: "jira/url", scope: "default", scope_id: 0 });
    expect(await screen.findByText("Override of jira/url removed.")).toBeInTheDocument();
  });

  it("Test POSTs the path and keeps the result as Last", async () => {
    const api = await stubApi({ ...base, "POST /api/admin/config/test": { status: "fail", code: "auth", message: "401 from Jira" } });
    renderAt("/admin/config", <Config />);
    await screen.findByText("Token");
    editOf("Token");
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Test" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/config/test")).toHaveLength(1));
    expect(api.calls("POST", "/api/admin/config/test")[0].body).toEqual({ path: "jira/token", scope: "default", scope_id: 0 });
    expect(await within(screen.getByRole("dialog")).findByText(/Last test:/)).toBeInTheDocument();
  });
});
