import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Agents } from "./Agents";
import { renderAt, stubApi, teardown } from "./testing";

afterEach(teardown);

describe("Agents", () => {
  it("lists agent views; the menu links to the view's config and copies the build command", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await stubApi({ "/api/admin/agents": [
      { id: 5, code: "dev", label: "Dev", workspace_code: "ws", ingress_count: 2, build_status: "ready" }] });
    renderAt("/admin/agents", <Agents />);
    expect(await screen.findByText("ready")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Actions for dev" }));
    expect(await screen.findByRole("menuitem", { name: "Open config" }))
      .toHaveAttribute("href", "/admin/config?scope=agent_view&scope_id=5");
    fireEvent.click(await screen.findByRole("menuitem", { name: "Copy build command" }));
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("bin/agento workspace:build --agent-view dev"));
  });
});
