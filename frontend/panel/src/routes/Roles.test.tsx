import { fireEvent, screen, within } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Users } from "./Users";
import { renderAt, renderInLocale, stubApi, teardown } from "./admin/testing";

const ROLES = [
  { code: "admin", label: "Administrator", builtin: true, users: 1, scopes: 0 },
  { code: "user", label: "User", builtin: true, users: 0, scopes: 2 },
  { code: "support", label: "Support", builtin: false, users: 0, scopes: 0 },
  { code: "ops", label: "Ops", builtin: false, users: 3, scopes: 1 },
];
const base = { "/api/admin/roles": ROLES, "/api/admin/users": [] as object[] };
afterEach(teardown);

const ROUTES = (
  <Routes>
    <Route path="/users" element={<Users />} />
    <Route path="/users/roles" element={<Users />} />
    <Route path="/users/roles/:code" element={<p>role page</p>} />
  </Routes>
);
const at = (path: string) => renderAt(path, ROUTES);
const refused = (code: string) => () => new Response(JSON.stringify({ error: "server words", code }), { status: 409 });
const row = (label: string) => screen.getAllByText(label).map((e) => e.closest("tr")).find(Boolean) as HTMLElement;

describe("Roles", () => {
  it("sends a role that holds only Roles from /users to the Roles tab", async () => {
    await stubApi(base, ["admin.roles"]);
    at("/users");
    expect(await screen.findByText("2 places")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Users" })).toBeNull();
  });

  it("shows each user's role label to a role that holds only Users", async () => {
    await stubApi({ ...base, "/api/admin/users": [{ id: 2, username: "bob", role: "ops", is_active: true }] }, ["admin.users"]);
    at("/users");
    expect(await screen.findByDisplayValue("Ops")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Roles" })).toBeNull();
  });

  it("lists roles on /users/roles with user count and access", async () => {
    await stubApi(base);
    at("/users/roles");
    expect(await screen.findByText("2 places")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Roles" })).toHaveAttribute("aria-selected", "true");
    expect(within(row("User")).getByText("2 places")).toBeInTheDocument();
    expect(within(row("Support")).getByText("No access yet")).toBeInTheDocument();
    expect(within(row("Administrator")).getByText("Built-in")).toBeInTheDocument();
  });

  it("writes a user count the way the Polish panel groups a number", async () => {
    await stubApi({ ...base, "/api/admin/roles": [{ code: "crowd", label: "Crowd", builtin: false, users: 12345, scopes: 1 }] });
    renderInLocale("pl", "/users/roles", ROUTES);
    await screen.findByRole("table");
    expect(within(row("Crowd")).getAllByRole("cell")[1].textContent).toBe(`12${String.fromCharCode(0xa0)}345`);
  });

  it("Delete is disabled, with the reason, for a built-in or a used role", async () => {
    const api = await stubApi({ ...base, "DELETE /api/admin/roles/support": null });
    at("/users/roles");
    await screen.findByText("2 places");
    const del = (label: string) => within(row(label)).getByRole("button", { name: "Delete" });
    expect(del("User")).toBeDisabled();
    expect(del("User")).toHaveAttribute("title", "A built-in role cannot be deleted.");
    expect(del("Ops")).toBeDisabled();
    expect(del("Ops")).toHaveAttribute("title", "3 users still have this role.");
    fireEvent.click(del("Support"));
    const confirm = await screen.findByRole("dialog", { name: "Delete this role?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete" }));
    await vi.waitFor(() => expect(api.calls("DELETE", "/api/admin/roles/support")).toHaveLength(1));
  });

  it("Add role derives the code from the name and opens the new role's resources", async () => {
    const api = await stubApi({ ...base,
      "POST /api/admin/roles": (b: { code: string; label: string }) => ({ ...b, builtin: false, users: 0, scopes: 0 }) });
    at("/users/roles");
    fireEvent.click(await screen.findByRole("button", { name: "Add role" }));
    const dialog = await screen.findByRole("dialog", { name: "Add role" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Support agent 2" } });
    expect(within(dialog).getByRole("textbox", { name: "Code" })).toHaveValue("support_agent_2");
    fireEvent.click(within(dialog).getByRole("button", { name: "Add role" }));
    await vi.waitFor(() => expect(api.calls("POST", "/api/admin/roles")).toHaveLength(1));
    expect(api.calls("POST", "/api/admin/roles")[0].body).toEqual({ code: "support_agent_2", label: "Support agent 2" });
    expect(await screen.findByText("role page")).toBeInTheDocument();
  });

  it("the user role select lists the roles from the API", async () => {
    await stubApi({ ...base, "/api/admin/users": [{ id: 2, username: "bob", role: "support", is_active: true }] });
    at("/users");
    expect(await screen.findByDisplayValue("Support")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("combobox", { name: "Role of bob" }));
    expect(await screen.findByRole("option", { name: "Ops" })).toBeInTheDocument();
  });

  it.each([["en", "Users", "Users · Agento", "Add a user", "Active"], ["pl", "Użytkownicy", "Użytkownicy · Agento", "Dodawanie użytkownika", "Aktywny"]])(
    "shows the Users screen in %s", async (tag, title, tab, form, status) => {
      await stubApi({ ...base, "/api/admin/users": [{ id: 2, username: "bob", role: "ops", is_active: true }] });
      renderInLocale(tag, "/users", ROUTES);
      expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
      expect(document.title).toBe(tab);
      expect(screen.getByText(form)).toBeInTheDocument();
      expect(await screen.findByText(status)).toBeInTheDocument();
    });

  it.each([["en", "Roles", "2 places", "Add role", "No access yet", "Users · Agento"],
    ["pl", "Role", "2 miejsca", "Dodaj rolę", "Brak dostępu", "Użytkownicy · Agento"]])(
    "shows the Roles tab in %s", async (tag, tab, places, add, none, title) => {
      await stubApi(base);
      renderInLocale(tag, "/users/roles", ROUTES);
      expect(await screen.findByText(places)).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByRole("button", { name: add })).toBeInTheDocument();
      expect(within(row("Support")).getByText(none)).toBeInTheDocument();
      expect(document.title).toBe(title);
    });

  it("a refused new user reads the Polish text of its code, never the server's words", async () => {
    await stubApi({ ...base, "POST /api/admin/users": refused("USERNAME_TAKEN") });
    renderInLocale("pl", "/users", ROUTES);
    fireEvent.change(screen.getByRole("textbox", { name: "Nazwa użytkownika" }), { target: { value: "bob" } });
    fireEvent.click(screen.getByRole("button", { name: "Dodaj użytkownika" }));
    expect(await screen.findByText("Użytkownik o tej nazwie już istnieje.")).toBeInTheDocument();
    expect(screen.queryByText("server words")).toBeNull();
  });

  it("a refused new role reads the Polish text of its code, never the server's words", async () => {
    await stubApi({ ...base, "POST /api/admin/roles": refused("ROLE_CODE_TAKEN") });
    renderInLocale("pl", "/users/roles", ROUTES);
    fireEvent.click(await screen.findByRole("button", { name: "Dodaj rolę" }));
    const dialog = await screen.findByRole("dialog", { name: "Dodaj rolę" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Nazwa" }), { target: { value: "Ops two" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Dodaj rolę" }));
    expect(await screen.findByText("Rola o tym kodzie już istnieje.")).toBeInTheDocument();
    expect(screen.queryByText("server words")).toBeNull();
  });
});
