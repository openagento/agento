import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Skills } from "./Skills";
import { renderAt, stubApi, teardown } from "./testing";

afterEach(teardown);

const SCOPES = { workspaces: [{ id: 4, code: "w", label: "W", is_active: true }], agent_views: [] };
const SKILLS = [
  { name: "pdf", path: "skill/pdf/is_enabled", enabled: true, explicit_here: true },
  { name: "xlsx", path: "skill/xlsx/is_enabled", enabled: false, explicit_here: false },
];

// The save contract lives in enablementScreens.test.tsx, against a server that remembers writes.
describe("Skills", () => {
  it("lists the skills of the URL scope, so a reload keeps it", async () => {
    const api = await stubApi({ "/api/admin/scopes": SCOPES, "/api/admin/skills": SKILLS });
    renderAt("/admin/skills?scope=workspace&scope_id=4", <Skills />);
    expect(await screen.findByRole("checkbox", { name: "pdf" })).toBeChecked();
    expect(api.calls("GET", "/api/admin/skills")[0].url.search).toBe("?scope=workspace&scope_id=4");
    expect(screen.getByRole("checkbox", { name: "xlsx" })).not.toBeChecked();
  });

  it("asks for a scope id before it loads anything", async () => {
    const api = await stubApi({ "/api/admin/scopes": { workspaces: [], agent_views: [] } });
    renderAt("/admin/skills?scope=agent_view", <Skills />);
    expect(await screen.findByText("Choose a scope")).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/skills")).toEqual([]);
  });
});
