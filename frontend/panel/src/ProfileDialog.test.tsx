import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect, useState } from "react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getLocale, getUser } from "@agento/api";
import { SessionI18n } from "./I18n";
import {
  CATALOG_VERSION, Providers, renderInLocale, servedCatalog, sessionLocale, stubApi, teardown, type SessionExtras,
} from "./routes/admin/testing";
import { Shell } from "./Shell";

afterEach(() => { teardown(); });

const english: SessionExtras = { ui_locale: null, locale: sessionLocale("en") };
const refusal = (status: number, body: unknown) => () => new Response(JSON.stringify(body), { status });
const savedAs = (tag: string) => () => ({ ui_locale: tag, locale: sessionLocale(tag) });

const mounts = vi.fn();
function Draft() {
  const [text, setText] = useState("");
  useEffect(() => { mounts(); document.title = "The screen below"; }, []);
  return <input aria-label="draft" value={text} onChange={(e) => setText(e.target.value)} />;
}
const FRAME = <Routes><Route path="/work" element={<Shell modules={[]} />}><Route index element={<Draft />} /></Route></Routes>;
const showFrame = () => render(<SessionI18n><Providers path="/work">{FRAME}</Providers></SessionI18n>);
const open = async (button: string, title: string) => {
  fireEvent.click(screen.getByRole("button", { name: button }));
  return screen.findByRole("dialog", { name: title });
};
const pickPolishAndSave = async () => {
  const dialog = await open("ann: your profile", "Profile");
  fireEvent.click(within(dialog).getByRole("combobox", { name: "Language" }));
  fireEvent.click(await screen.findByRole("option", { name: "Polski", hidden: true }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
  return dialog;
};

describe("the profile dialog", () => {
  it.each([
    ["en", "ann: your profile", "Profile", "Panel language", "Language", "English", "Save", "Close"],
    ["pl", "ann: Twój profil", "Profil", "Język panelu", "Język", "Polski", "Zapisz", "Zamknij"],
  ])("shows the language form in %s over the screen, and leaves the tab title", async (tag, button, title, section, label, own, save, close) => {
    await stubApi({}, [], { ui_locale: tag, locale: sessionLocale(tag) });
    renderInLocale(tag, "/work", FRAME);
    const dialog = await open(button, title);
    expect(within(dialog).getByRole("group", { name: section })).toBeInTheDocument();
    expect(within(dialog).getByRole("combobox", { name: label })).toHaveValue(own);
    expect(within(dialog).getByRole("button", { name: save })).toBeDisabled();
    expect(within(dialog).getAllByRole("button", { name: close })).toHaveLength(2);
    expect(screen.getByLabelText("draft", { selector: "input" })).toBeInTheDocument();
    expect(document.title).toBe("The screen below");
  });

  it.each([
    ["Escape", (dialog: HTMLElement) => fireEvent.keyDown(dialog, { key: "Escape" })],
    ["the Close button", (dialog: HTMLElement) => fireEvent.click(within(dialog).getAllByRole("button", { name: "Close" })[1])],
  ])("%s closes it and gives the focus back to the user name", async (_name, close) => {
    await stubApi({}, [], english);
    showFrame();
    const button = screen.getByRole("button", { name: "ann: your profile" });
    button.focus();
    close(await open("ann: your profile", "Profile"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(button).toHaveFocus());
  });

  it("a language change shows the new language at once, stays open, and keeps what the user typed below", async () => {
    mounts.mockClear();
    const api = await stubApi({ "/api/panel/i18n/pl": servedCatalog("pl"), "PATCH /api/profile/locale": savedAs("pl") }, [], english);
    showFrame();
    fireEvent.change(screen.getByLabelText("draft"), { target: { value: "half a sentence" } });
    await pickPolishAndSave();

    const dialog = await screen.findByRole("dialog", { name: "Profil" });
    expect(within(dialog).getByRole("combobox", { name: "Język" })).toHaveValue("Polski");
    expect(within(dialog).getByRole("button", { name: "Zapisz" })).toBeDisabled();
    expect(screen.getByLabelText("draft", { selector: "input" })).toHaveValue("half a sentence");
    expect(mounts).toHaveBeenCalledTimes(1);
    expect(document.documentElement.lang).toBe("pl");
    expect(document.title).toBe("The screen below");
    expect(await screen.findByText("Język panelu to teraz Polski.")).toBeInTheDocument();
    expect(api.calls("PATCH", "/api/profile/locale").map((c) => [c.body, c.headers["If-Match"]]))
      .toEqual([[{ ui_locale: "pl" }, `"${CATALOG_VERSION}"`]]);
    expect(getUser()?.ui_locale).toBe("pl");
    expect(api.calls("GET", "/api/session")).toHaveLength(0);
  });

  it("a refused save keeps the language, the session and the dialog, and says so in the dialog", async () => {
    await stubApi({
      "/api/panel/i18n/pl": servedCatalog("pl"),
      "PATCH /api/profile/locale": refusal(400, { error: "server words", code: "LOCALE_UNSUPPORTED" }),
    }, [], english);
    showFrame();
    const dialog = await pickPolishAndSave();

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("This language is not available.");
    expect(screen.queryByText(/server words/)).toBeNull();
    expect(within(dialog).getByRole("combobox", { name: "Language" })).toHaveValue("Polski");
    expect(document.documentElement.lang).toBe("en");
    expect(getLocale()?.effective).toBe("en");
    expect(getUser()?.ui_locale).toBeNull();
  });

  it("a catalog that changed twice between the read and the save says so, and Save tries again", async () => {
    const api = await stubApi({
      "/api/panel/i18n/pl": servedCatalog("pl"),
      "PATCH /api/profile/locale": refusal(409, { error: "server words", code: "CATALOG_CHANGED" }),
    }, [], english);
    showFrame();
    const dialog = await pickPolishAndSave();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The language texts changed. Try again.");
    expect(api.calls("PATCH", "/api/profile/locale")).toHaveLength(2);
    expect(document.documentElement.lang).toBe("en");
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("a save with no known code shows the dialog's own text", async () => {
    await stubApi({ "/api/panel/i18n/pl": servedCatalog("pl"), "PATCH /api/profile/locale": refusal(500, { error: "server words" }) }, [], english);
    showFrame();
    const dialog = await pickPolishAndSave();
    expect(await within(dialog).findByRole("alert"))
      .toHaveTextContent("The language could not be saved. The panel keeps the language it had.");
  });

  it("a catalog that cannot be read sends no save and keeps the dialog", async () => {
    const api = await stubApi({ "PATCH /api/profile/locale": savedAs("pl") }, [], english);
    showFrame();
    const dialog = await pickPolishAndSave();
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    expect(api.calls("PATCH", "/api/profile/locale")).toHaveLength(0);
    expect(within(dialog).getByRole("combobox", { name: "Language" })).toHaveValue("Polski");
  });

  /** A save that worked closes the dialog; the next opening is in the new language. */
  const afterSave = async (button: string, title: string) => {
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    return open(button, title);
  };

  it("a language that was shown before, and whose catalog cannot be read now, fails in the open and Save tries again", async () => {
    let readable = true;
    const api = await stubApi({
      "/api/panel/i18n/pl": () => (readable ? servedCatalog("pl") : refusal(503, { error: "server words", code: "SERVICE_UNAVAILABLE" })()),
      "/api/panel/i18n/en": servedCatalog("en"),
      "PATCH /api/profile/locale": (body: unknown) => savedAs((body as { ui_locale: string }).ui_locale)(),
    }, [], english);
    showFrame();
    await pickPolishAndSave();
    const polish = await afterSave("ann: Twój profil", "Profil");
    fireEvent.click(within(polish).getByRole("combobox", { name: "Język" }));
    fireEvent.click(await screen.findByRole("option", { name: "English", hidden: true }));
    fireEvent.click(within(polish).getByRole("button", { name: "Zapisz" }));
    const dialog = await afterSave("ann: your profile", "Profile");

    readable = false;
    fireEvent.click(within(dialog).getByRole("combobox", { name: "Language" }));
    fireEvent.click(await screen.findByRole("option", { name: "Polski", hidden: true }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The service is not available now. Try again later.");
    expect(screen.getByRole("dialog", { name: "Profile" })).toBe(dialog);
    expect(document.documentElement.lang).toBe("en");
    expect(getUser()?.ui_locale).toBe("en");
    expect(api.calls("PATCH", "/api/profile/locale")).toHaveLength(2);
    expect(screen.getAllByText("Język panelu to teraz Polski.")).toHaveLength(1);
    expect(within(dialog).getByRole("combobox", { name: "Language" })).toHaveValue("Polski");
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeEnabled();

    readable = true;
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.documentElement.lang).toBe("pl");
    expect(getUser()?.ui_locale).toBe("pl");
  });

  it("a stored language that the panel could not show keeps Save enabled, and a save shows it", async () => {
    await stubApi({ "/api/panel/i18n/pl": servedCatalog("pl"), "PATCH /api/profile/locale": savedAs("pl") }, [],
      { ui_locale: "pl", locale: sessionLocale("pl") });
    showFrame();
    const dialog = await open("ann: your profile", "Profile");
    expect(within(dialog).getByRole("combobox", { name: "Language" })).toHaveValue("Polski");
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    const polish = await afterSave("ann: Twój profil", "Profil");
    expect(within(polish).getByRole("button", { name: "Zapisz" })).toBeDisabled();
  });

  const closeAndReopen = async (dialog: HTMLElement) => {
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    return open("ann: your profile", "Profile");
  };

  it("a save that is pending when the dialog closes is still pending when it opens again: no second save", async () => {
    let answer: (saved: unknown) => void = () => undefined;
    const api = await stubApi({
      "/api/panel/i18n/pl": servedCatalog("pl"),
      "PATCH /api/profile/locale": () => new Promise((resolve) => { answer = resolve; }),
    }, [], english);
    showFrame();
    const first = await pickPolishAndSave();
    await waitFor(() => expect(api.calls("PATCH", "/api/profile/locale")).toHaveLength(1));

    const again = await closeAndReopen(first);
    expect(again).not.toBe(first);
    expect(within(again).getByRole("combobox", { name: "Language" })).toHaveValue("Polski");
    expect(within(again).getByRole("combobox", { name: "Language" })).toBeDisabled();
    expect(within(again).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(within(again).getByRole("status")).toHaveTextContent("The panel is saving the language Polski.");
    fireEvent.submit(within(again).getByRole("group", { name: "Panel language" }).closest("form")!);
    expect(api.calls("PATCH", "/api/profile/locale")).toHaveLength(1);

    answer(savedAs("pl")());
    const polish = await screen.findByRole("dialog", { name: "Profil" });
    expect(within(polish).getByRole("combobox", { name: "Język" })).toHaveValue("Polski");
    expect(within(polish).queryByRole("status")).toBeNull();
    expect(within(polish).getByRole("button", { name: "Zapisz" })).toBeDisabled();
    expect(await screen.findAllByText("Język panelu to teraz Polski.")).toHaveLength(1);
    expect(getUser()?.ui_locale).toBe("pl");
    expect(api.calls("PATCH", "/api/profile/locale")).toHaveLength(1);
  });

  it("a save that fails while the dialog is closed says so when it opens again, and the next close forgets it", async () => {
    let answer: (refused: Response) => void = () => undefined;
    await stubApi({
      "/api/panel/i18n/pl": servedCatalog("pl"),
      "PATCH /api/profile/locale": () => new Promise<Response>((resolve) => { answer = resolve; }),
    }, [], english);
    showFrame();
    fireEvent.keyDown(await pickPolishAndSave(), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    answer(refusal(400, { error: "server words", code: "LOCALE_UNSUPPORTED" })());

    const again = await open("ann: your profile", "Profile");
    expect(await within(again).findByRole("alert")).toHaveTextContent("This language is not available.");
    expect(within(again).getByRole("combobox", { name: "Language" })).toHaveValue("Polski");
    expect(within(again).getByRole("button", { name: "Save" })).toBeEnabled();
    expect(document.documentElement.lang).toBe("en");

    const clean = await closeAndReopen(again);
    expect(within(clean).queryByRole("alert")).toBeNull();
    expect(within(clean).getByRole("combobox", { name: "Language" })).toHaveValue("English");
  });

  it("says when the stored language is not installed, and which one the panel uses", async () => {
    await stubApi({}, [], { ui_locale: "fr", locale: sessionLocale("en") });
    renderInLocale("en", "/work", FRAME);
    const dialog = await open("ann: your profile", "Profile");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Your saved language (fr) is not installed here. The panel uses English.");
    expect(within(dialog).getByRole("combobox", { name: "Language" })).toHaveValue("English");
  });
});
