import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderAt, stubApi, teardown } from "../../../../../frontend/panel/src/routes/admin/testing";
import AppsPage from "./AppsPage";
import ApprovalPage from "./ApprovalPage";

const VIEWS = [{ id: 3, code: "support", label: "Support", workspace_id: 1 }, { id: 4, code: "ops", label: "", workspace_id: 1 }];
const V1 = "v-20261010-090200-77cd";
const V2 = "v-20261011-101500-ab12";
const CATALOGUE = [{ artifact_code: "approver", version_id: V1, title: "Jira approver", current_version: V2, update_waiting: true }];
const APPROVAL = {
  approval: { approved_version_id: V1, allowed_actions: ["jira_add_comment"], activated_by: "service:web:user:1", activated_at: "2026-10-09T10:00:00Z" },
  current: { version_id: V2, title: "Jira approver", actions: ["jira_add_comment", "csv_export"] },
};
const TOOLS = [{ toolset: "jira", tools: [{ name: "jira_add_comment", enabled: true }, { name: "csv_export", enabled: false }] }];

afterEach(() => { teardown(); vi.restoreAllMocks(); });

describe("the apps catalogue", () => {
  it("lists the first view's approved apps; an approver sees the waiting version and the approval link", async () => {
    const api = await stubApi({ "/api/miniapps/agent-views": VIEWS, "/api/miniapps/agent-views/3/catalogue": CATALOGUE, "/api/launches": [] });
    renderAt("/apps", <AppsPage />);
    expect(await screen.findByRole("link", { name: "Jira approver" })).toHaveAttribute("href", "/apps/approver?view=3");
    expect(screen.getByText("New version waiting")).toBeInTheDocument();
    expect(api.calls("GET", "/api/miniapps/agent-views/4/catalogue")).toEqual([]);
  });

  it("a user without the approve leaf sees plain titles and no waiting version", async () => {
    await stubApi({ "/api/miniapps/agent-views": VIEWS, "/api/miniapps/agent-views/3/catalogue": CATALOGUE, "/api/launches": [] }, ["miniapps"]);
    renderAt("/apps", <AppsPage />);
    expect(await screen.findByText("Jira approver")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Jira approver" })).toBeNull();
    expect(screen.queryByText("New version waiting")).toBeNull();
  });

  it("Open launches the app; a blocked window is reported", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    await stubApi({ "/api/miniapps/agent-views": VIEWS, "/api/miniapps/agent-views/3/catalogue": CATALOGUE, "/api/launches": [] });
    renderAt("/apps", <AppsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    expect(open).toHaveBeenCalledWith("about:blank", expect.any(String));
    expect(await screen.findByRole("alert")).toHaveTextContent("The browser blocked the window");
  });
});

const approvalAt = (path = "/apps/approver") => renderAt(path, <Routes><Route path="/apps/:code" element={<ApprovalPage />} /></Routes>);

describe("the approval page", () => {
  it("shows the approval, the current version's actions with the approved ones checked, and the tool state", async () => {
    await stubApi({ "/api/miniapps/agent-views": VIEWS, "/api/miniapps/apps/approver/views/3/approval": APPROVAL, "/api/admin/tools": TOOLS });
    approvalAt();
    expect(await screen.findByRole("checkbox", { name: "jira_add_comment" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "csv_export" })).not.toBeChecked();
    expect(screen.getByText(V1)).toBeInTheDocument();
    expect(screen.getByText(V2)).toBeInTheDocument();
    expect(await screen.findByText("off")).toBeInTheDocument();
  });

  it("starts on the view named in the link", async () => {
    const api = await stubApi({ "/api/miniapps/agent-views": VIEWS, "/api/miniapps/apps/approver/views/4/approval": APPROVAL, "/api/admin/tools": TOOLS });
    approvalAt("/apps/approver?view=4");
    expect(await screen.findByRole("checkbox", { name: "jira_add_comment" })).toBeChecked();
    expect(api.calls("GET", "/api/miniapps/apps/approver/views/3/approval")).toEqual([]);
  });

  it("starts on the first view when the linked view is not one of the user's", async () => {
    await stubApi({ "/api/miniapps/agent-views": VIEWS, "/api/miniapps/apps/approver/views/3/approval": APPROVAL, "/api/admin/tools": TOOLS });
    approvalAt("/apps/approver?view=99");
    expect(await screen.findByRole("checkbox", { name: "jira_add_comment" })).toBeChecked();
  });

  it("Approve sends current's id and the checked actions; a 409 is shown and the page reloads", async () => {
    let n = 0;
    const api = await stubApi({
      "/api/miniapps/agent-views": VIEWS, "/api/miniapps/apps/approver/views/3/approval": () => { n += 1; return APPROVAL; },
      "/api/admin/tools": TOOLS,
      "POST /api/miniapps/apps/approver/views/3/approval": new Response(JSON.stringify({ error: "current version changed, reload" }), { status: 409 }),
    });
    approvalAt();
    fireEvent.click(await screen.findByRole("checkbox", { name: "csv_export" }));
    fireEvent.click(screen.getByRole("button", { name: "Approve current" }));
    expect(await screen.findByText("current version changed, reload")).toBeInTheDocument();
    expect(api.calls("POST", "/api/miniapps/apps/approver/views/3/approval")[0].body)
      .toEqual({ expected_version_id: V2, actions: ["jira_add_comment", "csv_export"] });
    await waitFor(() => expect(n).toBeGreaterThan(1));
  });

  it("Revoke deletes the approval; with no approval every action starts checked and Revoke is gone", async () => {
    const api = await stubApi({
      "/api/miniapps/agent-views": VIEWS, "/api/admin/tools": TOOLS,
      "/api/miniapps/apps/approver/views/3/approval": () => (api.calls("DELETE", "/api/miniapps/apps/approver/views/3/approval").length
        ? { ...APPROVAL, approval: null } : APPROVAL),
      "DELETE /api/miniapps/apps/approver/views/3/approval": null,
    });
    approvalAt();
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull());
    expect(screen.getByRole("checkbox", { name: "csv_export" })).toBeChecked();
  });

  it("Preview launches current with no actions", async () => {
    vi.spyOn(window, "open").mockReturnValue({ close: vi.fn(), closed: false } as unknown as Window);
    const api = await stubApi({
      "/api/miniapps/agent-views": VIEWS, "/api/miniapps/apps/approver/views/3/approval": APPROVAL, "/api/admin/tools": TOOLS,
      "POST /api/miniapps/launches": new Response(JSON.stringify({ error: "no" }), { status: 503 }),
    });
    approvalAt();
    fireEvent.click(await screen.findByRole("button", { name: "Preview current" }));
    await waitFor(() => expect(api.calls("POST", "/api/miniapps/launches")[0]?.body)
      .toEqual({ agent_view_id: 3, artifact_code: "approver", preview: true }));
  });
});
