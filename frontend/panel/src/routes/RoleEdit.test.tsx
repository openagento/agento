import { fireEvent, screen, within } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RoleEdit } from "./RoleEdit";
import { renderAt, stubApi, teardown } from "./admin/testing";

const SCOPES = {
  workspaces: [{ id: 2, code: "ws", label: "WS", is_active: true }],
  agent_views: [{ id: 3, code: "av", label: "AV", workspace_id: 2, is_active: true }],
};
const ROLE = { code: "support", label: "Support", builtin: false, users: 0,
  scopes: [{ workspace_id: null, agent_view_id: 3, tools: 1, operations: 0 }] };
const tool = (name: string, over: object = {}) => ({ name, enabled: true, granted: false, inherited: false, ...over });
const RESOURCES = {
  operations: [
    { id: "artifact.launch", title: "Launch a miniapp", granted: false, inherited: false, builtin: false },
    { id: "conversation.run_details", title: "See run details", granted: false, inherited: false, builtin: true },
  ],
  toolsets: [{ toolset: "jira", tools: [
    tool("jira_search", { granted: true }), tool("jira_comment"), tool("jira_get", { inherited: true }),
    tool("jira_delete", { enabled: false }),
  ] }],
};
const base = {
  "/api/admin/scopes": SCOPES, "/api/admin/roles/support": ROLE,
  "/api/admin/roles/support/resources": RESOURCES,
  "PUT /api/admin/roles/support/resources": { added: 1, removed: 0 },
};
afterEach(teardown);

const at = (path: string) => renderAt(path, <Routes><Route path="/users/roles/:code" element={<RoleEdit />} /></Routes>);
const box = (name: string) => screen.findByRole("checkbox", { name });
const save = () => fireEvent.click(screen.getByRole("button", { name: "Save" }));

describe("RoleEdit", () => {
  it("opens on the scope with access and saves the checked tree with one PUT", async () => {
    const api = await stubApi(base);
    at("/users/roles/support?tab=resources");
    expect(await box("jira_search")).toBeChecked();
    expect(api.calls("GET", "/api/admin/roles/support/resources")[0].url.search).toBe("?scope=agent_view&scope_id=3");
    expect(screen.queryByText(/not saved/)).toBeNull();
    fireEvent.click(await box("jira_comment"));
    fireEvent.click(await box("Launch a miniapp"));
    expect(screen.getByText("2 changes not saved")).toBeInTheDocument();
    save();
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/roles/support/resources")).toHaveLength(1));
    expect(api.calls("PUT", "/api/admin/roles/support/resources")[0].body).toEqual({
      scope: "agent_view", scope_id: 3, tools: ["jira_search", "jira_comment"], operations: ["artifact.launch"],
    });
  });

  it("inherited and built-in leaves are checked, locked and never sent", async () => {
    const api = await stubApi(base);
    at("/users/roles/support?tab=resources");
    expect(await box("jira_get")).toBeChecked();
    expect(await box("jira_get")).toBeDisabled();
    expect(await box("See run details")).toBeDisabled();
    expect(screen.getByText("Via workspace")).toBeInTheDocument();
    expect(screen.getByText("Off here")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(await box("jira_get")).toBeChecked();
    save();
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/roles/support/resources")).toHaveLength(1));
    expect(api.calls("PUT", "/api/admin/roles/support/resources")[0].body).toMatchObject({ tools: [], operations: [] });
  });

  it("locks the tree and the scope while a save runs, and keeps nothing stale after it", async () => {
    let release!: () => void;
    const done = new Promise<void>((r) => { release = r; });
    const api = await stubApi({ ...base, "PUT /api/admin/roles/support/resources": async () => { await done; return { added: 1, removed: 0 }; } });
    at("/users/roles/support?tab=resources");
    fireEvent.click(await box("jira_comment"));
    save();
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/roles/support/resources")).toHaveLength(1));
    expect(await box("Launch a miniapp")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Clear" })).toBeDisabled();
    expect(screen.getAllByLabelText("Scope")[0]).toBeDisabled();
    release();
    await vi.waitFor(() => expect(screen.queryByText(/not saved/)).toBeNull());
    expect(await box("Launch a miniapp")).toBeEnabled();
  });

  it("a search hides leaves but keeps their checks in the payload", async () => {
    const api = await stubApi(base);
    at("/users/roles/support?tab=resources");
    fireEvent.click(await box("jira_comment"));
    fireEvent.change(screen.getByRole("textbox", { name: "Search resources" }), { target: { value: "launch" } });
    await vi.waitFor(() => expect(screen.queryByRole("checkbox", { name: "jira_comment" })).toBeNull());
    fireEvent.click(await box("Launch a miniapp"));
    save();
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/roles/support/resources")).toHaveLength(1));
    expect(api.calls("PUT", "/api/admin/roles/support/resources")[0].body).toMatchObject({
      tools: ["jira_search", "jira_comment"], operations: ["artifact.launch"],
    });
  });

  it("a group checkbox toggles its unlocked leaves", async () => {
    await stubApi(base);
    at("/users/roles/support?tab=resources");
    const group = await box("jira");
    expect(group).toHaveAttribute("data-indeterminate", "true");
    fireEvent.click(group);
    expect(await box("jira_delete")).toBeChecked();
    expect(within(group.closest("[role=treeitem]") as HTMLElement).getAllByText("4 of 4")).toHaveLength(1);
  });

  it("switching the scope with unsaved changes asks first, then loads the other scope", async () => {
    const api = await stubApi(base);
    at("/users/roles/support?tab=resources");
    fireEvent.click(await box("jira_comment"));
    fireEvent.click(screen.getByRole("combobox", { name: "Scope" }));
    fireEvent.click(await screen.findByRole("option", { name: /ws — WS/ }));
    const confirm = await screen.findByRole("dialog", { name: "Discard changes?" });
    expect(api.calls("GET", "/api/admin/roles/support/resources")).toHaveLength(1);
    fireEvent.click(within(confirm).getByRole("button", { name: "Discard" }));
    await vi.waitFor(() => expect(api.calls("GET", "/api/admin/roles/support/resources")).toHaveLength(2));
    expect(api.calls("GET", "/api/admin/roles/support/resources")[1].url.search).toBe("?scope=workspace&scope_id=2");
    expect(screen.queryByText(/not saved/)).toBeNull();
  });

  it("Role info renames with PATCH; the code is read-only", async () => {
    const api = await stubApi({ ...base, "PATCH /api/admin/roles/support": { ...ROLE, label: "Helpdesk" } });
    at("/users/roles/support");
    fireEvent.change(await screen.findByRole("textbox", { name: "Name" }), { target: { value: "Helpdesk" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(api.calls("PATCH", "/api/admin/roles/support")).toHaveLength(1));
    expect(api.calls("PATCH", "/api/admin/roles/support")[0].body).toEqual({ label: "Helpdesk" });
    expect(screen.getByRole("textbox", { name: "Code" })).toHaveAttribute("readonly");
  });

  it("Role info keeps what the operator types while the role refetches after a save", async () => {
    let release!: () => void;
    const done = new Promise<void>((r) => { release = r; });
    let saved = false;
    await stubApi({ ...base,
      "PATCH /api/admin/roles/support": () => { saved = true; return { ...ROLE, label: "Helpdesk" }; },
      "/api/admin/roles/support": async () => { if (!saved) return ROLE; await done; return { ...ROLE, label: "Helpdesk" }; },
    });
    at("/users/roles/support");
    const name = await screen.findByRole("textbox", { name: "Name" });
    fireEvent.change(name, { target: { value: "Helpdesk" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(screen.getByRole("textbox", { name: "Name" })).toBeEnabled());
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "Helpdesk EU" } });
    release();
    await screen.findByRole("heading", { name: /Helpdesk/ });
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("Helpdesk EU");
  });

  it("the admin role says what it has built in", async () => {
    await stubApi({ ...base, "/api/admin/roles/admin": { ...ROLE, code: "admin", label: "Administrator", builtin: true } });
    at("/users/roles/admin?tab=resources");
    expect(await screen.findByText(/Administrator has user management/)).toBeInTheDocument();
  });
});
