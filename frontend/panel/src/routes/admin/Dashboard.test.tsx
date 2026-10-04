import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Home } from "../Home";
import { renderAt, stubApi, teardown } from "./testing";

const DASH = {
  db_connected: true, running_jobs: 4, version: "0.17.0", python_version: "3.12.1", module_count: 23,
  recent_jobs: [{ id: 9, type: "cron", status: "RUNNING", reference_id: "AG-9", agent_view_code: "av",
    created_at: "2026-10-02T10:00:00Z", finished_at: null }],
  credentials: [{ id: 1, scope: "claude", label: "main", status: "error", enabled: true,
    error_source: "auto", used_at: null, expires_at: null }],
  agent_views: [{ id: 3, code: "dev", label: "Developer", workspace_id: 1 }],
};
afterEach(teardown);

describe("Home", () => {
  it("is the dashboard for an admin", async () => {
    await stubApi({ "/api/admin/dashboard": DASH });
    renderAt("/", <Home />);
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Python 3.12.1")).toBeInTheDocument();
    expect(screen.getByText("AG-9")).toBeInTheDocument();
    expect(screen.getByText("Developer")).toBeInTheDocument();
    expect(screen.getByText("error")).toBeInTheDocument();
  });

  it("is a greeting for a user, with no admin request", async () => {
    const api = await stubApi({}, "user");
    renderAt("/", <Home />);
    expect(await screen.findByText("Hello, ann")).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/dashboard")).toEqual([]);
  });
});
