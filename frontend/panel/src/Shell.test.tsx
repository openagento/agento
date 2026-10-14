import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AdminNav } from "./Shell";
import { renderAt, stubApi, teardown } from "./routes/admin/testing";

afterEach(teardown);

describe("the Administration nav group", () => {
  it("lists every admin screen for an admin", async () => {
    await stubApi({});
    renderAt("/admin/jobs", <AdminNav onNavigate={() => {}} />);
    expect(screen.getByText("Administration")).toBeInTheDocument();
    const links = Object.fromEntries(screen.getAllByRole("link").map((a) => [a.textContent, a.getAttribute("href")]));
    expect(links).toEqual({
      Dashboard: "/", Jobs: "/admin/jobs", Agents: "/admin/agents", Credentials: "/admin/credentials",
      Tools: "/admin/tools", Skills: "/admin/skills", Config: "/admin/config", Users: "/users",
    });
    expect(screen.getByRole("link", { name: "Jobs" })).toHaveAttribute("data-active");
  });

  it("lists only the screens a role holds", async () => {
    await stubApi({}, ["admin.jobs", "admin.users"]);
    renderAt("/", <AdminNav onNavigate={() => {}} />);
    expect(screen.getAllByRole("link").map((a) => a.textContent)).toEqual(["Jobs", "Users"]);
  });

  it("opens Users for a role that holds only Roles", async () => {
    await stubApi({}, ["admin.roles"]);
    renderAt("/", <AdminNav onNavigate={() => {}} />);
    expect(screen.getAllByRole("link").map((a) => a.textContent)).toEqual(["Users"]);
  });

  it("is hidden when no admin leaf is held", async () => {
    await stubApi({}, []);
    renderAt("/", <AdminNav onNavigate={() => {}} />);
    expect(screen.queryByText("Administration")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
