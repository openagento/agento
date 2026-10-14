import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { APP_ICON_NAMES } from "@agento/ui";
import { renderAt, stubApi, teardown } from "../../../../../frontend/panel/src/routes/admin/testing";
import AppsPage, { type App } from "./AppsPage";
import AppSettingsPage from "./AppSettingsPage";
import icons from "./icons.json";

const VIEWS = [{ id: 3, code: "support", label: "Support", workspace_id: 1 }, { id: 4, code: "ops", label: "", workspace_id: 1 }];
const V1 = "v-20261010-090200-77cd";
const V2 = "v-20261011-101500-ab12";
const view = (id: number, label: string, execute = true) => ({ agent_view_id: id, label, version_id: V1, execute });
const APPS: App[] = [
  { artifact_code: "approver", title: "Jira approver", icon: "mail", can: { view: true, edit: true, remove: true, execute: true },
    views: [{ ...view(3, "Support"), update_waiting: true, approve_again: false }] },
  { artifact_code: "report", title: "Report", icon: null, can: { view: true, edit: false, remove: false, execute: false },
    views: [view(3, "Support", false)] },
  { artifact_code: "multi", title: "Multi", icon: "table", can: { view: true, edit: false, remove: false, execute: true },
    views: [view(3, "Support"), view(4, "Ops")] },
];
const APPROVAL = {
  approval: { approved_version_id: V1, allowed_actions: ["jira_add_comment"], activated_by: "service:web:user:1", activated_at: "2026-10-09T10:00:00Z" },
  current: { version_id: V2, title: "Jira approver", actions: ["jira_add_comment", "csv_export"] },
};
const TOOLS = [{ toolset: "jira", tools: [{ name: "jira_add_comment", enabled: true }, { name: "csv_export", enabled: false }] }];
const APP = {
  artifact_code: "approver", default_title: "Jira approver", title: null, icon: "mail",
  can: { view: true, edit: true, remove: true },
  members: [{ id: 9, user_id: 5, username: "bob", role: null, can_edit: true, can_remove: false }],
};

afterEach(() => { teardown(); vi.restoreAllMocks(); localStorage.clear(); });

const appsApi = (extra: Record<string, unknown> = {}) => stubApi({ "/api/miniapps/apps": APPS, "/api/launches": [], ...extra });

describe("the apps catalogue", () => {
  it("the panel icon set is the one routes.py accepts", () => {
    expect(APP_ICON_NAMES).toEqual(icons);
  });

  it("shows tiles by default: a view-only app is a disabled tile, settings only with edit", async () => {
    await appsApi();
    renderAt("/apps", <AppsPage />);
    const tiles = await screen.findByRole("list", { name: "Apps" });
    expect(within(tiles).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Report" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "Report" })).toHaveAttribute("title", "You may view this app but not run it");
    expect(screen.getAllByRole("button", { name: "Settings" })).toHaveLength(1);
    expect(screen.getByText("New version waiting")).toBeInTheDocument();
  });

  it("the list toggle shows a table and is remembered", async () => {
    await appsApi();
    renderAt("/apps", <AppsPage />);
    fireEvent.click(await screen.findByText("List"));
    expect(await screen.findByRole("table", { name: "Apps" })).toBeInTheDocument();
    expect(localStorage.getItem("agento.apps.view")).toBe("list");
    teardown();
    await appsApi();
    renderAt("/apps", <AppsPage />);
    expect(await screen.findByRole("table", { name: "Apps" })).toBeInTheDocument();
  });

  it("a tile opens its one view; a blocked window is reported", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    await appsApi();
    renderAt("/apps", <AppsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Jira approver" }));
    expect(open).toHaveBeenCalledWith("about:blank", expect.any(String));
    expect(await screen.findByRole("alert")).toHaveTextContent("The browser blocked the window");
  });

  it("a tile with several views asks which one", async () => {
    vi.spyOn(window, "open").mockReturnValue({ close: vi.fn(), closed: false } as unknown as Window);
    const api = await appsApi({ "POST /api/miniapps/launches": new Response(JSON.stringify({ error: "no" }), { status: 503 }) });
    renderAt("/apps", <AppsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Multi" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Ops" }));
    await waitFor(() => expect(api.calls("POST", "/api/miniapps/launches")[0]?.body)
      .toEqual({ agent_view_id: 4, artifact_code: "multi" }));
  });

  it("the list opens an app with several views from the open icon's menu", async () => {
    vi.spyOn(window, "open").mockReturnValue({ close: vi.fn(), closed: false } as unknown as Window);
    const api = await appsApi({ "POST /api/miniapps/launches": new Response(JSON.stringify({ error: "no" }), { status: 503 }) });
    localStorage.setItem("agento.apps.view", "list");
    renderAt("/apps", <AppsPage />);
    const row = (await screen.findByText("Multi")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Open" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Ops" }));
    await waitFor(() => expect(api.calls("POST", "/api/miniapps/launches")[0]?.body)
      .toEqual({ agent_view_id: 4, artifact_code: "multi" }));
  });

  it("delete asks first, then deletes the app", async () => {
    const api = await appsApi({ "DELETE /api/miniapps/apps/approver": null });
    renderAt("/apps", <AppsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    expect(await screen.findByText(/Are you sure you want to delete Jira approver\? This deletes the app and all its versions\./))
      .toBeInTheDocument();
    expect(api.calls("DELETE", "/api/miniapps/apps/approver")).toEqual([]);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.calls("DELETE", "/api/miniapps/apps/approver")).toHaveLength(1));
  });
});

const settingsAt = (path = "/apps/approver") => renderAt(path, <Routes><Route path="/apps/:code" element={<AppSettingsPage />} /></Routes>);
const settingsApi = (extra: Record<string, unknown> = {}) => stubApi({
  "/api/miniapps/apps/approver": APP, "/api/miniapps/agent-views": VIEWS,
  "/api/miniapps/apps/approver/views/3/approval": APPROVAL, "/api/admin/tools": TOOLS,
  "/api/miniapps/apps/approver/member-options": { users: ["bob", "carol"], roles: [{ code: "user", label: "User" }] }, ...extra,
});

describe("the app settings page", () => {
  it("edits the title in place: Enter saves, Escape cancels", async () => {
    const api = await settingsApi({ "PATCH /api/miniapps/apps/approver": null });
    settingsAt();
    fireEvent.click(await screen.findByRole("button", { name: "Edit title" }));
    const input = screen.getByRole("textbox", { name: "Title" });
    expect(input).toHaveValue("Jira approver");
    fireEvent.change(input, { target: { value: "Approver" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: "Title" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Title" }), { target: { value: " Approver " } });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Title" }), { key: "Enter" });
    await waitFor(() => expect(api.calls("PATCH", "/api/miniapps/apps/approver").map((c) => c.body)).toEqual([{ title: "Approver" }]));
  });

  it("picks the icon from the popover and saves it", async () => {
    const api = await settingsApi({ "PATCH /api/miniapps/apps/approver": null });
    settingsAt();
    fireEvent.click(await screen.findByRole("button", { name: "Change icon" }));
    fireEvent.click(await screen.findByRole("radio", { name: "table" }));
    await waitFor(() => expect(api.calls("PATCH", "/api/miniapps/apps/approver")[0]?.body).toEqual({ icon: "table" }));
  });

  it("has no delete: the apps list deletes", async () => {
    await settingsApi();
    settingsAt();
    await screen.findByRole("button", { name: "Edit title" });
    expect(screen.queryByRole("button", { name: /Delete/ })).toBeNull();
  });

  it("adds a member from the searchable list and changes a flag", async () => {
    const api = await settingsApi({ "PUT /api/miniapps/apps/approver/members": null });
    settingsAt();
    const field = await screen.findByLabelText("Add a user or a role", { selector: "input" });
    fireEvent.click(field);
    fireEvent.change(field, { target: { value: "car" } });
    expect(screen.queryByRole("option", { name: "bob" })).toBeNull(); // already a member
    fireEvent.click(await screen.findByRole("option", { name: "carol" }));
    fireEvent.click(screen.getByRole("button", { name: "Add member" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Remove: bob" }));
    await waitFor(() => expect(api.calls("PUT", "/api/miniapps/apps/approver/members").map((c) => c.body)).toEqual([
      { username: "carol", can_edit: false, can_remove: false },
      { username: "bob", can_edit: true, can_remove: true },
    ]));
  });

  it("a view-only user sees no form", async () => {
    await settingsApi({ "/api/miniapps/apps/approver": { ...APP, can: { view: true, edit: false, remove: false }, members: null } });
    settingsAt();
    expect(await screen.findByText("You may open this app, but not change it.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit title" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Change icon" })).toBeNull();
  });

  it("shows the approval, the current version's actions with the approved ones checked, and the tool state", async () => {
    await settingsApi();
    settingsAt();
    expect(await screen.findByRole("checkbox", { name: "jira_add_comment" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "csv_export" })).not.toBeChecked();
    expect(screen.getByText(V1)).toBeInTheDocument();
    expect(await screen.findByText("off")).toBeInTheDocument();
  });

  it("starts on the view named in the link", async () => {
    const api = await settingsApi({ "/api/miniapps/apps/approver/views/4/approval": APPROVAL });
    settingsAt("/apps/approver?view=4");
    expect(await screen.findByRole("checkbox", { name: "jira_add_comment" })).toBeChecked();
    expect(api.calls("GET", "/api/miniapps/apps/approver/views/3/approval")).toEqual([]);
  });

  it("Approve sends current's id and the checked actions; a 409 is shown", async () => {
    const api = await settingsApi({
      "POST /api/miniapps/apps/approver/views/3/approval": new Response(JSON.stringify({ error: "current version changed, reload" }), { status: 409 }),
    });
    settingsAt();
    fireEvent.click(await screen.findByRole("checkbox", { name: "csv_export" }));
    fireEvent.click(screen.getByRole("button", { name: "Approve current" }));
    expect(await screen.findByText("current version changed, reload")).toBeInTheDocument();
    expect(api.calls("POST", "/api/miniapps/apps/approver/views/3/approval")[0].body)
      .toEqual({ expected_version_id: V2, actions: ["jira_add_comment", "csv_export"] });
  });

  it("Revoke asks first, then deletes the approval", async () => {
    const api = await settingsApi({ "DELETE /api/miniapps/apps/approver/views/3/approval": null });
    settingsAt();
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(api.calls("DELETE", "/api/miniapps/apps/approver/views/3/approval")).toEqual([]);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(api.calls("DELETE", "/api/miniapps/apps/approver/views/3/approval")).toHaveLength(1));
  });

  it("Preview launches current with no actions", async () => {
    vi.spyOn(window, "open").mockReturnValue({ close: vi.fn(), closed: false } as unknown as Window);
    const api = await settingsApi({ "POST /api/miniapps/previews": new Response(JSON.stringify({ error: "no" }), { status: 503 }) });
    settingsAt();
    fireEvent.click(await screen.findByRole("button", { name: "Preview current" }));
    await waitFor(() => expect(api.calls("POST", "/api/miniapps/previews")[0]?.body)
      .toEqual({ agent_view_id: 3, artifact_code: "approver" }));
  });
});

describe("the link to the artifact", () => {
  const ARTIFACTS = "/api/versioned_artifacts/artifacts";
  it("the settings page and the list reach the artifact's versions when the artifacts panel answers", async () => {
    await settingsApi({ [ARTIFACTS]: [] });
    settingsAt();
    expect(await screen.findByRole("link", { name: "Versions and history" })).toHaveAttribute("href", "/artifacts/approver");

    teardown();
    await appsApi({ [ARTIFACTS]: [] });
    renderAt("/apps", <AppsPage />);
    fireEvent.click(await screen.findByText("List"));
    expect((await screen.findAllByRole("link", { name: "Versions" }))[0]).toHaveAttribute("href", "/artifacts/approver");
  });

  it("shows no such link when the artifacts panel refuses (module off, or no leaf)", async () => {
    await settingsApi({ [ARTIFACTS]: new Response(JSON.stringify({ error: "forbidden" }), { status: 403 }) });
    settingsAt();
    await screen.findByRole("checkbox", { name: "jira_add_comment" });
    await waitFor(() => expect(screen.queryByRole("link", { name: "Versions and history" })).toBeNull());
  });
});
