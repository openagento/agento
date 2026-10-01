import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { endSession, login, QueryClientProvider, queryClient } from "@agento/api";
import { Grants } from "./Grants";

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(async () => {
  fetchMock = vi.fn(async (url: string | URL, init?: RequestInit) => new Response(
    String(url).endsWith("/api/session") ? JSON.stringify({ user: { id: 1 }, csrf_token: "t", expires_at: "x" })
      : init?.method === "POST" ? "{}" : "[]", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  await login("a", "b");
});
afterEach(() => { endSession(); vi.unstubAllGlobals(); queryClient.clear(); });

const posts = () => fetchMock.mock.calls.filter(([u, i]) => String(u).endsWith("/api/admin/grants") && i?.method === "POST");

describe("Grants form", () => {
  it("refuses a mistyped scope id and sends nothing; a valid id is sent", async () => {
    render(<MantineProvider><QueryClientProvider client={queryClient}><Grants /></QueryClientProvider></MantineProvider>);
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "miniapp_list" } });
    const view = screen.getByRole("textbox", { name: "Agent view id" });
    fireEvent.change(view, { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Add grant" }));
    expect(await screen.findByText(/positive whole number/)).toBeInTheDocument();
    expect(posts()).toEqual([]);

    fireEvent.change(view, { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Add grant" }));
    await vi.waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(String(posts()[0][1]!.body))).toEqual({ role: "user", kind: "tool", name: "miniapp_list", agent_view_id: 7 });
  });
});
