import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentoUiProvider } from "@agento/ui";
import { getLocale, login } from "@agento/api";
import { CATALOG_DEADLINE_MS, ensureCatalog } from "../catalogStore";
import { SessionI18n } from "../I18n";
import {
  servedBootstrap, servedCatalog, sessionBody, sessionLocale, stalledBody, stubRoutes, teardown, UNREADABLE_CATALOGS,
} from "./admin/testing";
import { Home } from "./Home";
import { Login } from "./Login";

afterEach(() => { teardown(); vi.useRealTimers(); });

const refusal = (status: number, body: unknown) => () => new Response(JSON.stringify(body), { status });
const show = () => render(
  <AgentoUiProvider env="test">
    <SessionI18n>
      <MemoryRouter initialEntries={["/login"]}>
        <Routes><Route path="/login" element={<Login />} /><Route path="/" element={<Home />} /></Routes>
      </MemoryRouter>
    </SessionI18n>
  </AgentoUiProvider>,
);
const signIn = async (submit: string) => {
  fireEvent.change(await screen.findByLabelText(/^(User name|Nazwa użytkownika)/), { target: { value: "ann" } });
  fireEvent.change(screen.getByLabelText(/^(Password|Hasło)/), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: submit }));
};

describe("Login", () => {
  it.each([
    ["en", "Sign in to Agento", "Sign in", "Sign in · Agento"],
    ["pl", "Zaloguj się do Agento", "Zaloguj się", "Logowanie · Agento"],
  ])("shows the form in the instance language (%s) and names the tab", async (tag, title, submit, tab) => {
    stubRoutes({ "GET /api/panel/bootstrap": () => servedBootstrap(tag) });
    show();
    expect(await screen.findByRole("group", { name: title })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: submit })).toBeInTheDocument();
    expect(document.title).toBe(tab);
    expect(document.documentElement.lang).toBe(tag);
  });

  it("shows no form before the language is known, so English never flashes", async () => {
    let answer!: (body: unknown) => void;
    stubRoutes({ "GET /api/panel/bootstrap": () => new Promise((resolve) => { answer = resolve; }) });
    show();
    await vi.waitFor(() => expect(answer).toBeDefined());
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByText(/Sign in/)).toBeNull();
    answer(servedBootstrap("pl"));
    expect(await screen.findByRole("button", { name: "Zaloguj się" })).toBeInTheDocument();
  });

  it("a refused language read shows the bundled English form", async () => {
    stubRoutes({ "GET /api/panel/bootstrap": refusal(500, { error: "boom" }) });
    show();
    expect(await screen.findByRole("group", { name: "Sign in to Agento" })).toBeInTheDocument();
  });

  it("a language read that takes too long shows the bundled English form at the deadline", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    show();
    await act(() => vi.advanceTimersByTimeAsync(CATALOG_DEADLINE_MS - 1));
    expect(screen.queryByRole("textbox")).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByRole("group", { name: "Sign in to Agento" })).toBeInTheDocument();
  });

  it.each(UNREADABLE_CATALOGS)("an answer that is not a catalog (%s) shows the bundled English form", async (_name, answer) => {
    stubRoutes({ "GET /api/panel/bootstrap": answer });
    show();
    expect(await screen.findByRole("group", { name: "Sign in to Agento" })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
  });

  it("an answer whose body never ends shows the bundled English form at the deadline", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => stalledBody(init)));
    show();
    await act(() => vi.advanceTimersByTimeAsync(CATALOG_DEADLINE_MS - 1));
    expect(screen.queryByRole("textbox")).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByRole("group", { name: "Sign in to Agento" })).toBeInTheDocument();
  });

  it.each([
    ["a wrong password", 401, { error: "invalid credentials", code: "AUTH_INVALID_CREDENTIALS" }, "Nazwa użytkownika lub hasło są nieprawidłowe."],
    ["a throttled sign-in", 429, { error: "too many attempts", code: "AUTH_THROTTLED" }, "Zbyt wiele nieudanych prób logowania. Spróbuj ponownie później."],
    ["a rate limit", 429, { error: "too many requests", code: "RATE_LIMITED" }, "Zbyt wiele żądań. Spróbuj ponownie później."],
    ["a code the panel does not know", 400, { error: "server words", code: "BRAND_NEW_CODE" }, "Logowanie nie powiodło się."],
    ["an answer with no code", 401, { error: "server words" }, "Logowanie nie powiodło się."],
  ])("%s shows its Polish text, never the server's", async (_name, status, body, text) => {
    stubRoutes({ "GET /api/panel/bootstrap": () => servedBootstrap("pl"), "POST /api/session": refusal(status, body) });
    show();
    await signIn("Zaloguj się");
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByText(/server words|invalid credentials|too many/)).toBeNull();
  });

  it("a refused sign-in of a signed-in user keeps the page language of the form, not the ended session's", async () => {
    let answer: () => unknown = () => sessionBody({ ui_locale: "de", locale: sessionLocale("de") });
    stubRoutes({
      "GET /api/panel/bootstrap": () => servedBootstrap("pl"),
      "POST /api/session": () => answer(),
      "GET /api/panel/i18n/de": () => ({ ...servedCatalog("pl"), locale: "de" }),
    });
    await login("ann", "pw");
    await ensureCatalog(getLocale());
    show();
    expect(await screen.findByRole("group", { name: "Zaloguj się do Agento" })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("pl");
    answer = refusal(401, { error: "invalid credentials", code: "AUTH_INVALID_CREDENTIALS" });
    await signIn("Zaloguj się");
    expect(await screen.findByRole("alert")).toHaveTextContent("Nazwa użytkownika lub hasło są nieprawidłowe.");
    expect(document.documentElement.lang).toBe("pl");
  });

  it("a sign-in lands on a Polish screen with no manual switch, also when the versions are equal", async () => {
    const api = stubRoutes({
      "GET /api/panel/bootstrap": () => servedBootstrap("en"),
      "POST /api/session": () => sessionBody({ ui_locale: "pl", locale: sessionLocale("pl") }),
      "GET /api/panel/i18n/pl": () => servedCatalog("pl"),
    });
    show();
    await signIn("Sign in");
    expect(await screen.findByRole("heading", { name: "Witaj, ann" })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("pl");
    expect(document.title).toBe("Witaj, ann · Agento");
    expect(api.paths()).toEqual(["GET /api/panel/bootstrap", "POST /api/session", "GET /api/panel/i18n/pl"]);
  });

  it("a sign-in whose catalog cannot be read still lands, in English", async () => {
    stubRoutes({
      "GET /api/panel/bootstrap": () => servedBootstrap("pl"),
      "POST /api/session": () => sessionBody({ ui_locale: "pl", locale: sessionLocale("pl") }),
    });
    show();
    await signIn("Zaloguj się");
    expect(await screen.findByRole("heading", { name: "Hello, ann" })).toBeInTheDocument();
  });
});
