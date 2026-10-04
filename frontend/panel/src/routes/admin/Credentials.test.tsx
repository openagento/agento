import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Credentials } from "./Credentials";
import { renderAt, stubApi, teardown } from "./testing";

const cred = (id: number, over: object = {}) => ({
  id, scope: "claude", label: `c${id}`, status: "ok", enabled: true, error_source: null,
  used_at: null, expires_at: null, token_limit: 1000, tokens_used: 100, call_count: 3, pct_free: 90, ...over,
});
afterEach(teardown);

describe("Credentials", () => {
  it("Clear error shows only on an error row and POSTs after the confirm", async () => {
    const api = await stubApi({
      "/api/admin/credentials": [cred(1), cred(2, { status: "error", error_source: "auto" })],
      "POST /api/admin/credentials/2/clear-error": null,
    });
    renderAt("/admin/credentials", <Credentials />);
    const buttons = await screen.findAllByRole("button", { name: "Clear error" });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    const dialog = await screen.findByRole("dialog");
    expect(api.calls("POST", "/api/admin/credentials/2/clear-error")).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Clear error" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/credentials/2/clear-error")).toHaveLength(1));
    expect(await screen.findByText("Error cleared on c2.")).toBeInTheDocument();
  });

  it("Disable sends nothing on cancel", async () => {
    const api = await stubApi({ "/api/admin/credentials": [cred(1)] });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(api.calls("POST", "/api/admin/credentials/1/disable")).toEqual([]);
  });

  it("an error row names the CLI and the TUI, never an error message", async () => {
    await stubApi({ "/api/admin/credentials": [
      // A server that leaked error_msg would still not get it rendered.
      { ...cred(3, { status: "error", error_source: "operator" }), error_msg: "sk-secret-leak" }] });
    renderAt("/admin/credentials", <Credentials />);
    expect(await screen.findByText(/bin\/agento credential:list or the admin TUI/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Usage" }));
    expect(await within(await screen.findByRole("dialog")).findByText(/Set by an operator/)).toBeInTheDocument();
    expect(screen.queryByText(/sk-secret-leak/)).toBeNull();
  });
});
