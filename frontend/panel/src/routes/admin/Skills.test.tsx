import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Skills } from "./Skills";
import { renderAt, stubApi, teardown } from "./testing";

afterEach(teardown);

describe("Skills", () => {
  it("lists skills at the URL scope and a toggle PUTs the skill gate", async () => {
    const api = await stubApi({
      "/api/admin/scopes": { workspaces: [{ id: 4, code: "w", label: "W", is_active: true }], agent_views: [] },
      "/api/admin/skills": [{ name: "pdf", path: "skill/pdf/is_enabled", enabled: true, explicit_here: true }],
      "PUT /api/admin/config": { path: "skill/pdf/is_enabled", reset: [] },
    });
    renderAt("/admin/skills?scope=workspace&scope_id=4", <Skills />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "pdf" }));
    await vi.waitFor(() => expect(api.calls("PUT", "/api/admin/config")).toHaveLength(1));
    expect(api.calls("GET", "/api/admin/skills")[0].url.search).toBe("?scope=workspace&scope_id=4");
    expect(api.calls("PUT", "/api/admin/config")[0].body)
      .toEqual({ path: "skill/pdf/is_enabled", value: "0", scope: "workspace", scope_id: 4 });
  });

  it("asks for a scope id before it loads anything", async () => {
    const api = await stubApi({ "/api/admin/scopes": { workspaces: [], agent_views: [] } });
    renderAt("/admin/skills?scope=agent_view", <Skills />);
    expect(await screen.findByText("Choose a scope")).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/skills")).toEqual([]);
  });
});
