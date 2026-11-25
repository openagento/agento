import { useEffect } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { Route, Routes, useLocation, useNavigate } from "react-router";
import { apiFetch, getLocale, getUser, onExpired, type PanelModule } from "@agento/api";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ensureCatalog } from "./catalogStore";
import { SessionI18n } from "./I18n";
import { AdminNav, ModuleNav, Shell } from "./Shell";
import { Providers, renderAt, renderInLocale, servedCatalog, sessionLocale, stubApi, teardown } from "./routes/admin/testing";

afterEach(() => { teardown(); });

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

describe("the panel frame in Polish", () => {
  it("names every admin screen with the glossary term", async () => {
    await stubApi({});
    renderInLocale("pl", "/admin/jobs", <AdminNav onNavigate={() => {}} />);
    expect(screen.getByText("Administracja")).toBeInTheDocument();
    expect(screen.getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Pulpit", "Zadania", "Agenci", "Poświadczenia", "Narzędzia", "Umiejętności", "Konfiguracja", "Użytkownicy",
    ]);
  });

  it("shows the frame's own texts, and the user name is the button of the profile settings", async () => {
    await stubApi({}, []);
    renderInLocale("pl", "/", <Routes><Route path="/" element={<Shell modules={[]} />} /></Routes>);
    expect(screen.getByRole("navigation", { name: "Główna" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Strona główna" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("button", { name: "Wyloguj się" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "ann: Twój profil" })).toHaveAttribute("aria-haspopup", "dialog");
  });

  it("a module's nav item reads `<module>:nav.<route path>`, and keeps the module's own text with no such key", async () => {
    const module: PanelModule = {
      contractVersion: 1, id: "notes", availability: { probe: "/api/notes" }, routes: [
        { path: "/notes", nav: "Notes", load: async () => ({ default: () => null }) },
        { path: "/notes/archive", nav: "Archive", load: async () => ({ default: () => null }) },
      ],
    };
    await stubApi({ "/api/notes": [] });
    renderInLocale("pl", "/", <ModuleNav module={module} onNavigate={() => {}} />, { notes: { "nav./notes": "Notatki" } });
    expect(await screen.findByRole("link", { name: "Notatki" })).toHaveAttribute("href", "/notes");
    expect(screen.getByRole("link", { name: "Archive" })).toHaveAttribute("href", "/notes/archive");
  });

  it("a sign-out goes to the sign-in page with no English frame on the way", async () => {
    await stubApi({ "/api/panel/i18n/pl": servedCatalog("pl"), "DELETE /api/session": null }, [],
      { ui_locale: "pl", locale: sessionLocale("pl") });
    await ensureCatalog(getLocale());
    render(
      <SessionI18n>
        <Providers path="/">
          <Routes><Route path="/" element={<Shell modules={[]} />} /><Route path="/login" element={<p>the sign-in page</p>} /></Routes>
        </Providers>
      </SessionI18n>,
    );
    const shown: string[] = [];
    const watch = new MutationObserver(() => shown.push(document.body.textContent ?? ""));
    watch.observe(document.body, { childList: true, subtree: true, characterData: true });
    fireEvent.click(screen.getByRole("button", { name: "Wyloguj się" }));
    expect(await screen.findByText("the sign-in page")).toBeInTheDocument();
    watch.disconnect();
    expect(shown.filter((text) => /Sign out|Home|theme/.test(text))).toEqual([]);
  });
  it("a sign-out that fails says so and stays on the page; the button works again and the next sign-out leaves", async () => {
    let answer: (response: Response) => void = () => undefined;
    const deletes = () => fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE");
    const { fetchMock } = await stubApi({ "/api/panel/i18n/pl": servedCatalog("pl") }, [], { ui_locale: "pl", locale: sessionLocale("pl") });
    const answered = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: string | URL, init?: RequestInit) => (init?.method === "DELETE"
      ? new Promise<Response>((resolve) => { answer = resolve; })
      : answered(input, init)));
    await ensureCatalog(getLocale());
    render(
      <SessionI18n>
        <Providers path="/">
          <Routes><Route path="/" element={<Shell modules={[]} />} /><Route path="/login" element={<p>the sign-in page</p>} /></Routes>
        </Providers>
      </SessionI18n>,
    );
    const button = () => screen.getByRole("button", { name: "Wyloguj się" });
    fireEvent.click(button());
    await vi.waitFor(() => expect(deletes()).toHaveLength(1));
    expect(button()).toHaveAttribute("data-disabled", "true");
    fireEvent.click(button());
    expect(deletes()).toHaveLength(1);

    answer(new Response(JSON.stringify({ error: "boom" }), { status: 500 }));
    expect(await screen.findByText("Wylogowanie nie powiodło się. Spróbuj ponownie.")).toBeInTheDocument();
    expect(screen.queryByText("the sign-in page")).toBeNull();
    expect(getUser()?.username).toBe("ann");
    expect(button()).not.toHaveAttribute("data-disabled");

    fireEvent.click(button());
    await vi.waitFor(() => expect(deletes()).toHaveLength(2));
    answer(new Response(null, { status: 204 }));
    expect(await screen.findByText("the sign-in page")).toBeInTheDocument();
    // The sign-in page starts clean: no message of the session that ended stays on it.
    await vi.waitFor(() => expect(screen.queryByText("Wylogowanie nie powiodło się. Spróbuj ponownie.")).toBeNull());
  });
  it("a sign-out leaves with no failure text when the server says that the session is already gone (401)", async () => {
    let answer: (response: Response) => void = () => undefined;
    const { fetchMock } = await stubApi({}, []);
    const answered = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: string | URL, init?: RequestInit) => (init?.method === "DELETE"
      ? new Promise<Response>((resolve) => { answer = resolve; })
      : answered(input, init)));
    renderAt("/", <Routes><Route path="/" element={<Shell modules={[]} />} /><Route path="/login" element={<p>the sign-in page</p>} /></Routes>);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true));
    answer(new Response(JSON.stringify({ error: "not signed in", code: "AUTH_REQUIRED" }), { status: 401 }));
    expect(await screen.findByText("the sign-in page")).toBeInTheDocument();
    expect(screen.queryByText("Sign-out failed. Try again.")).toBeNull();
    expect(getUser()).toBeNull();
  });
  // The session's end by a 401 of another request owns the navigation (`onExpired`, as router.tsx).
  it.each([
    ["answers", () => new Response(null, { status: 204 })],
    ["fails", () => new Response(JSON.stringify({ error: "boom" }), { status: 500 })],
  ])("a sign-out that %s after the session expired changes nothing on the sign-in page", async (_name, response) => {
    function ExpiryOwner() {
      const navigate = useNavigate();
      useEffect(() => onExpired(() => { void navigate("/login?next=%2Fjobs"); }), [navigate]);
      return null;
    }
    const SignIn = () => <p>the sign-in page {useLocation().search}</p>;
    let answer: (response: Response) => void = () => undefined;
    const { fetchMock } = await stubApi({}, []);
    const answered = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: string | URL, init?: RequestInit) => {
      if (init?.method === "DELETE") return new Promise<Response>((resolve) => { answer = resolve; });
      if (String(input).endsWith("/api/expired")) return Promise.resolve(new Response(JSON.stringify({ error: "not signed in", code: "AUTH_REQUIRED" }), { status: 401 }));
      return answered(input, init);
    });
    renderAt("/", <><ExpiryOwner /><Routes><Route path="/" element={<Shell modules={[]} />} /><Route path="/login" element={<SignIn />} /></Routes></>);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true));
    await apiFetch("/api/expired").catch(() => undefined);
    expect(await screen.findByText("the sign-in page ?next=%2Fjobs")).toBeInTheDocument();
    answer(response());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByText("the sign-in page ?next=%2Fjobs")).toBeInTheDocument();
    expect(screen.queryByText("Sign-out failed. Try again.")).toBeNull();
  });
});
