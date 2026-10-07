import { fireEvent, screen, within } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Users } from "./Users";
import { renderAt, stubApi, teardown } from "./admin/testing";

const ROLES = [
  { code: "admin", label: "Administrator", builtin: true, users: 1, scopes: 0 },
  { code: "user", label: "User", builtin: true, users: 0, scopes: 2 },
  { code: "support", label: "Support", builtin: false, users: 0, scopes: 0 },
  { code: "ops", label: "Ops", builtin: false, users: 3, scopes: 1 },
];
const base = { "/api/admin/roles": ROLES, "/api/admin/users": [] as object[] };
afterEach(teardown);

const at = (path: string) => renderAt(path, (
  <Routes>
    <Route path="/users" element={<Users />} />
    <Route path="/users/roles" element={<Users />} />
    <Route path="/users/roles/:code" element={<p>role page</p>} />
  </Routes>
));
const row = (label: string) => screen.getAllByText(label).map((e) => e.closest("tr")).find(Boolean) as HTMLElement;

describe("Roles", () => {
  it("lists roles on /users/roles with user count and access", async () => {
    await stubApi(base);
    at("/users/roles");
    expect(await screen.findByText("2 places")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Roles" })).toHaveAttribute("aria-selected", "true");
    expect(within(row("User")).getByText("2 places")).toBeInTheDocument();
    expect(within(row("Support")).getByText("No access yet")).toBeInTheDocument();
    expect(within(row("Administrator")).getByText("Built-in")).toBeInTheDocument();
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
});
