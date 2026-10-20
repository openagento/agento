import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Skills } from "./Skills";
import { renderAt, stubApi, teardown } from "./testing";

afterEach(teardown);

// The save contract and the URL scope live in enablementScreens.test.tsx, against a server that remembers writes.
describe("Skills", () => {
  it("asks for a scope id before it loads anything", async () => {
    const api = await stubApi({ "/api/admin/scopes": { workspaces: [], agent_views: [] } });
    renderAt("/admin/skills?scope=agent_view", <Skills />);
    expect(await screen.findByText("Choose a scope")).toBeInTheDocument();
    expect(api.calls("GET", "/api/admin/skills")).toEqual([]);
  });
});
