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

  it("is hidden for a user", async () => {
    await stubApi({}, "user");
    renderAt("/", <AdminNav onNavigate={() => {}} />);
    expect(screen.queryByText("Administration")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
