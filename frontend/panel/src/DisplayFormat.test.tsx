import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { boot, endSession, refreshDisplay } from "@agento/api";
import { Timestamp } from "@agento/ui";
import { SessionDisplayFormat } from "./DisplayFormat";

afterEach(() => { endSession(); vi.unstubAllGlobals(); });

const session = (display: unknown) => vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
  user: { id: 1, username: "ann", role: "admin", is_active: true }, csrf_token: "t", expires_at: "x", display,
}))));
const ui = <SessionDisplayFormat><Timestamp value="2026-10-05T08:23:44Z" /></SessionDisplayFormat>;

describe("SessionDisplayFormat", () => {
  it("formats every Timestamp with the session's display settings", async () => {
    session({ date_format: "iso", timezone: "UTC" });
    await boot();
    render(ui);
    expect(screen.getByText("2026-10-05 08:23:44")).toBeInTheDocument();
  });

  it("with no display settings keeps the browser's locale string", async () => {
    session(null);
    await boot();
    render(ui);
    expect(screen.getByText(new Date("2026-10-05T08:23:44Z").toLocaleString())).toBeInTheDocument();
  });

  it("a changed setting reaches a mounted Timestamp without a reload", async () => {
    session({ date_format: "us", timezone: "UTC" });
    await boot();
    render(ui);
    expect(screen.getByText("10/5/2026, 8:23:44 AM")).toBeInTheDocument();
    session({ date_format: "eu", timezone: "UTC" });
    await act(() => refreshDisplay());
    expect(screen.getByText("5.10.2026, 8:23:44 AM")).toBeInTheDocument();
  });
});
