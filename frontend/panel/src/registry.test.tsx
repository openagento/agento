import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClientProvider, queryClient, type PanelModule } from "@agento/api";
import { AgentoUiProvider } from "@agento/ui";
import { ModuleRoute } from "./ModuleRoute";
import { ModuleNav } from "./Shell";

const mod: PanelModule = {
  contractVersion: 1, id: "demo", availability: { probe: "/api/demo/ping" },
  routes: [{ path: "/demo", nav: "Demo", load: async () => ({ default: () => <p>demo screen</p> }) }],
};
const Screen = () => <p>demo screen</p>;
const wrap = (ui: ReactNode) => render(
  <AgentoUiProvider><QueryClientProvider client={queryClient}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider></AgentoUiProvider>,
);

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => { queryClient.clear(); fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => vi.unstubAllGlobals());

describe("a module's availability probe", () => {
  it("404 hides the nav entry and the route shows NotAvailable", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 404 }));
    wrap(<><ModuleNav module={mod} onNavigate={() => {}} /><ModuleRoute module={mod} Screen={Screen} /></>);
    expect(await screen.findByText("Not available")).toBeInTheDocument();
    expect(screen.queryByText("Demo")).toBeNull();
    expect(screen.queryByText("demo screen")).toBeNull();
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/demo/ping");
  });

  it("2xx shows the nav entry and the screen", async () => {
    fetchMock.mockResolvedValue(new Response("[]", { status: 200 }));
    wrap(<><ModuleNav module={mod} onNavigate={() => {}} /><ModuleRoute module={mod} Screen={Screen} /></>);
    expect(await screen.findByText("demo screen")).toBeInTheDocument();
    expect(screen.getByText("Demo")).toBeInTheDocument();
  });

  it("a network failure fails closed", async () => {
    fetchMock.mockRejectedValue(new TypeError("offline"));
    wrap(<ModuleRoute module={mod} Screen={Screen} />);
    expect(await screen.findByText("Not available")).toBeInTheDocument();
  });
});
