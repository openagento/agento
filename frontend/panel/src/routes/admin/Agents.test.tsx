import { fireEvent, screen } from "@testing-library/react";
import { useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Agents } from "./Agents";
import { renderAt, renderInLocale, stubApi, teardown } from "./testing";

afterEach(teardown);

const AGENT = { id: 5, code: "dev", label: "Dev", workspace_code: "ws", ingress_count: 2, build_status: "ready" };
const refused = (body: object) => () => new Response(JSON.stringify(body), { status: 403 });

function Where() {
  const l = useLocation();
  return <output data-testid="where">{`${l.pathname}${l.search}`}</output>;
}

describe("Agents", () => {
  it("lists agent views; the row actions are icons that open the config and copy the build command", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await stubApi({ "/api/admin/agents": [AGENT] });
    renderAt("/admin/agents", <><Agents /><Where /></>);
    expect(await screen.findByText("ready")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for dev" })).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Copy build command" }));
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("bin/agento workspace:build --agent-view dev"));

    fireEvent.click(screen.getByRole("button", { name: "Open config" }));
    await vi.waitFor(() => expect(screen.getByTestId("where"))
      .toHaveTextContent("/admin/config?scope=agent_view&scope_id=5"));
  });

  it.each([
    ["en", "Agents", "Workspace", "Open config", "Agents · Agento"],
    ["pl", "Agenci", "Obszar roboczy", "Otwórz konfigurację", "Agenci · Agento"],
  ])("shows the screen in %s", async (tag, title, column, action, tab) => {
    await stubApi({ "/api/admin/agents": [AGENT] });
    renderInLocale(tag, "/admin/agents", <Agents />);
    expect(await screen.findByRole("button", { name: action })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: column })).toBeInTheDocument();
    expect(document.title).toBe(tab);
  });

  it.each([
    ["a known code", { error: "server words", code: "FORBIDDEN" }, "Nie masz uprawnień do tej operacji."],
    ["no code", { error: "server words" }, "Nie udało się wczytać widoków agentów."],
  ])("a list refused with %s shows Polish text, never the server's", async (_name, body, text) => {
    await stubApi({ "/api/admin/agents": refused(body) });
    renderInLocale("pl", "/admin/agents", <Agents />);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByText(/server words/)).toBeNull();
  });

  it("tells in Polish that the build command was copied", async () => {
    Object.defineProperty(navigator, "clipboard", { value: { writeText: vi.fn(async () => undefined) }, configurable: true });
    await stubApi({ "/api/admin/agents": [AGENT] });
    renderInLocale("pl", "/admin/agents", <Agents />);
    fireEvent.click(await screen.findByRole("button", { name: "Kopiuj polecenie budowania" }));
    expect(await screen.findByText(/^Skopiowano bin\/agento workspace:build --agent-view dev\./)).toBeInTheDocument();
  });
});
