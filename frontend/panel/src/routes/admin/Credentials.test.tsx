import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Credentials, freeColor } from "./Credentials";
import { renderAt, stubApi, teardown } from "./testing";

const cred = (id: number, over: object = {}) => ({
  id, scope: "claude", label: `c${id}`, status: "ok", enabled: true, error_source: null,
  used_at: null, expires_at: null, token_limit: 1000, tokens_used: 100, call_count: 3, pct_free: 90,
  type: "oauth", limits: null, limits_at: null, ...over,
});
const login = (over: object = {}) => ({
  status: "waiting", verify_url: null, user_code: null, needs_code: false, error_code: null,
  expires_at: "2026-10-04T12:15:00Z", ...over,
});
const two = { windows: [
  { label: "5h", used_pct: 25, resets_at: "2026-10-04T15:00:00Z" },
  { label: "Week", used_pct: 92.4, resets_at: null }], balance_usd: null };
afterEach(teardown);

describe("freeColor", () => {
  it("is green from 50% free, yellow from 20% to 49%, red below 20%", () => {
    expect([100, 50, 49, 45, 20, 19, 0].map(freeColor))
      .toEqual(["green", "green", "yellow", "yellow", "yellow", "red", "red"]);
  });
});

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

  it("Free shows one bar per window, a balance, or a dash", async () => {
    await stubApi({ "/api/admin/credentials": [
      cred(1, { limits: two, limits_at: "2026-10-04T12:00:00Z" }),
      cred(2, { type: "openrouter_api_key", limits: { windows: [], balance_usd: 12.3456 } }),
      cred(3, { type: "anthropic_api_key", used_at: "2026-10-04T11:00:00Z", expires_at: "2026-11-04T11:00:00Z" }),
      cred(4, { limits: { windows: [], balance_usd: null } }),
      cred(5, { limits: null, limits_at: "2026-10-05T08:00:00Z" })] });
    renderAt("/admin/credentials", <Credentials />);
    expect(await screen.findByRole("progressbar", { name: "c1 5h free" })).toHaveAttribute("aria-valuenow", "75");
    expect(screen.getByRole("progressbar", { name: "c1 Week free" })).toHaveAttribute("aria-valuenow", "8");
    expect(screen.getByText("75%")).toBeInTheDocument();
    expect(screen.getByText("8%")).toBeInTheDocument();
    expect(screen.getByText("$12.35")).toBeInTheDocument();
    // rows[0] is the header; c3 has no limits, and its Free cell is its only dash.
    expect(within(screen.getAllByRole("row")[3]).getAllByText("—")).toHaveLength(1);
    // A check that ran and failed is not "does not apply".
    expect(within(screen.getAllByRole("row")[5]).getByText("Check failed")).toBeInTheDocument();
    // The local 24h share is no longer a column.
    expect(screen.queryByText("90%")).toBeNull();
  });

  it("the drawer shows the local limit, each window with its reset, and when it was checked", async () => {
    await stubApi({ "/api/admin/credentials": [cred(1, { limits: two, limits_at: "2026-10-04T12:00:00Z" })] });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Usage" }));
    const d = within(await screen.findByRole("dialog"));
    expect(d.getByText("Local limit (24h)")).toBeInTheDocument();
    expect(d.getByText("90%")).toBeInTheDocument();
    expect(d.getByText("5h")).toBeInTheDocument();
    expect(d.getByText(/75% free, resets/)).toBeInTheDocument();
    expect(d.getByText(/8% free, reset time unknown/)).toBeInTheDocument();
    expect(d.getByText("Checked")).toBeInTheDocument();
  });

  it("Re-login shows only on an enabled OAuth credential", async () => {
    await stubApi({ "/api/admin/credentials": [
      cred(1), cred(2, { type: "anthropic_api_key" }), cred(3, { enabled: false })] });
    renderAt("/admin/credentials", <Credentials />);
    expect(await screen.findAllByRole("button", { name: "Re-login" })).toHaveLength(1);
  });

  it("a code flow posts the pasted code", async () => {
    const api = await stubApi({
      "/api/admin/credentials": [cred(1)],
      "POST /api/admin/credentials/1/login": { id: 7 },
      "/api/admin/credential-logins/7": login({ verify_url: "https://claude.ai/oauth/authorize?x=1", needs_code: true }),
      "POST /api/admin/credential-logins/7/code": null,
    });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Re-login" }));
    const d = within(await screen.findByRole("dialog"));
    const link = await d.findByRole("link", { name: "Open the login page" });
    expect(link).toHaveAttribute("href", "https://claude.ai/oauth/authorize?x=1");
    const field = d.getByLabelText("Paste the code from the login page");
    fireEvent.change(field, { target: { value: "has space" } });
    fireEvent.click(d.getByRole("button", { name: "Save" }));
    expect(await d.findByText(/no spaces/)).toBeInTheDocument();
    expect(api.calls("POST", "/api/admin/credential-logins/7/code")).toEqual([]);
    fireEvent.change(field, { target: { value: "abc#state" } });
    fireEvent.click(d.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/credential-logins/7/code")).toHaveLength(1));
    expect(api.calls("POST", "/api/admin/credential-logins/7/code")[0].body).toEqual({ code: "abc#state" });
    expect(await d.findByText("Checking…")).toBeInTheDocument();
  });

  it("a device flow shows the link in a new tab and the user code", async () => {
    await stubApi({
      "/api/admin/credentials": [cred(1)],
      "POST /api/admin/credentials/1/login": { id: 8 },
      "/api/admin/credential-logins/8": login({ verify_url: "https://auth.openai.com/codex/device", user_code: "ABCD-EFGH" }),
    });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Re-login" }));
    const d = within(await screen.findByRole("dialog"));
    const link = await d.findByRole("link", { name: "Open the login page" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(d.getByText("ABCD-EFGH")).toBeInTheDocument();
    expect(d.getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(d.queryByLabelText("Paste the code from the login page")).toBeNull();
  });

  it("done says signed in and refreshes the list", async () => {
    const api = await stubApi({
      "/api/admin/credentials": [cred(1)],
      "POST /api/admin/credentials/1/login": { id: 9 },
      "/api/admin/credential-logins/9": login({ status: "done" }),
    });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Re-login" }));
    expect(await screen.findByText("Signed in. The credential is saved.")).toBeInTheDocument();
    await vi.waitFor(() => expect(api.calls("GET", "/api/admin/credentials")).toHaveLength(2));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close" }));
    expect(api.calls("POST", "/api/admin/credential-logins/9/cancel")).toEqual([]);
  });

  it("a failed login shows a fixed sentence and Try again starts a new one", async () => {
    const api = await stubApi({
      "/api/admin/credentials": [cred(1)],
      "POST /api/admin/credentials/1/login": { id: 10 },
      "/api/admin/credential-logins/10": login({ status: "failed", error_code: "busy" }),
    });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Re-login" }));
    expect(await screen.findByText(/A run was using the credential/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/credentials/1/login")).toHaveLength(2));
  });

  it("a refused start shows the server message", async () => {
    await stubApi({
      "/api/admin/credentials": [cred(1)],
      "POST /api/admin/credentials/1/login": new Response(JSON.stringify({ error: "A login is already running." }), { status: 409 }),
    });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Re-login" }));
    expect(await within(await screen.findByRole("dialog")).findByText("A login is already running.")).toBeInTheDocument();
  });

  it("closing while active cancels the login", async () => {
    const api = await stubApi({
      "/api/admin/credentials": [cred(1)],
      "POST /api/admin/credentials/1/login": { id: 11 },
      "/api/admin/credential-logins/11": login({ verify_url: "https://auth.openai.com/x", user_code: "ABCD-EFGH" }),
      "POST /api/admin/credential-logins/11/cancel": null,
    });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Re-login" }));
    const d = within(await screen.findByRole("dialog"));
    await d.findByText("ABCD-EFGH");
    fireEvent.click(d.getByRole("button", { name: "Close" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/credential-logins/11/cancel")).toHaveLength(1));
  });

  it("closing before the first status answer still cancels", async () => {
    const api = await stubApi({
      "/api/admin/credentials": [cred(1)],
      "POST /api/admin/credentials/1/login": { id: 12 },
      "/api/admin/credential-logins/12": new Promise(() => undefined),
      "POST /api/admin/credential-logins/12/cancel": null,
    });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Re-login" }));
    const d = within(await screen.findByRole("dialog"));
    await vi.waitFor(() => expect(api.calls("GET", "/api/admin/credential-logins/12")).toHaveLength(1));
    fireEvent.click(d.getByRole("button", { name: "Close" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/credential-logins/12/cancel")).toHaveLength(1));
  });

  it("a start that answers after its modal closed is cancelled, even when the same credential was reopened", async () => {
    let first: (v: unknown) => void = () => undefined;
    let starts = 0;
    const api = await stubApi({
      "/api/admin/credentials": [cred(1)],
      "POST /api/admin/credentials/1/login": () => (++starts === 1
        ? new Promise((resolve) => { first = resolve; })
        : new Response(JSON.stringify({ error: "A login is already running." }), { status: 409 })),
      "POST /api/admin/credential-logins/21/cancel": null,
    });
    renderAt("/admin/credentials", <Credentials />);
    fireEvent.click(await screen.findByRole("button", { name: "Re-login" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Close" }));
    await vi.waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Re-login" }));
    await within(await screen.findByRole("dialog")).findByText("A login is already running.");
    first({ id: 21 });
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/credential-logins/21/cancel")).toHaveLength(1));
  });
});
