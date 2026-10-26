import { fireEvent, screen } from "@testing-library/react";
import { useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Agents } from "./Agents";
import { renderAt, stubApi, teardown } from "./testing";

afterEach(teardown);

function Where() {
  const l = useLocation();
  return <output data-testid="where">{`${l.pathname}${l.search}`}</output>;
}

describe("Agents", () => {
  it("lists agent views; the row actions are icons that open the config and copy the build command", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await stubApi({ "/api/admin/agents": [
      { id: 5, code: "dev", label: "Dev", workspace_code: "ws", ingress_count: 2, build_status: "ready" }] });
    renderAt("/admin/agents", <><Agents /><Where /></>);
    expect(await screen.findByText("ready")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for dev" })).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Copy build command" }));
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("bin/agento workspace:build --agent-view dev"));

    fireEvent.click(screen.getByRole("button", { name: "Open config" }));
    await vi.waitFor(() => expect(screen.getByTestId("where"))
      .toHaveTextContent("/admin/config?scope=agent_view&scope_id=5"));
  });
});
