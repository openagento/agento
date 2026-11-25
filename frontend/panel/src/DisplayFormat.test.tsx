import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { boot, endSession, refreshDisplay } from "@agento/api";
import { I18nProvider, Timestamp } from "@agento/ui";
import { SessionDisplayFormat } from "./DisplayFormat";

afterEach(() => { endSession(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const session = (display: unknown) => vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
  user: { id: 1, username: "ann", role: "admin", is_active: true, ui_locale: null }, csrf_token: "t", expires_at: "x", display,
}))));
const ui = <SessionDisplayFormat><Timestamp value="2026-10-05T08:23:44Z" /></SessionDisplayFormat>;

describe("SessionDisplayFormat", () => {
  it("formats every Timestamp with the session's display settings", async () => {
    session({ date_format: "iso", timezone: "UTC" });
    await boot();
    render(ui);
    expect(screen.getByText("2026-10-05 08:23:44")).toBeInTheDocument();
  });

  it.each([
    ["en", "10/5/2026, 8:23:44 AM"],
    ["pl", "5.10.2026, 08:23:44"],
  ])("with no display settings, an account in %s gets its own date in the browser's zone, whatever the browser's language", async (locale, text) => {
    vi.stubGlobal("navigator", { ...navigator, language: "de-DE", languages: ["de-DE"] });
    const zoned = Intl.DateTimeFormat;
    vi.spyOn(Intl, "DateTimeFormat").mockImplementation(function inBrowserZoneUtc(tag?: string, options?: Intl.DateTimeFormatOptions) {
      return new zoned(tag, { ...options, timeZone: options?.timeZone ?? "UTC" });
    } as typeof Intl.DateTimeFormat);
    session(null);
    await boot();
    render(<I18nProvider locale={locale} messages={{}}>{ui}</I18nProvider>);
    expect(screen.getByText(text)).toBeInTheDocument();
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

  const inLanguage = (locale: string) => <I18nProvider locale={locale} messages={{}}>{ui}</I18nProvider>;

  it.each([
    ["en", "10/5/2026, 8:23:44 AM"],
    ["pl", "5.10.2026, 08:23:44"],
  ])("with no format set, %s writes a date its own way", async (locale, text) => {
    session({ date_format: "us", timezone: "UTC", date_format_explicit: false });
    await boot();
    render(inLanguage(locale));
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it.each(["en", "pl"])("a format an operator set wins over the language (%s)", async (locale) => {
    session({ date_format: "iso", timezone: "UTC", date_format_explicit: true });
    await boot();
    render(inLanguage(locale));
    expect(screen.getByText("2026-10-05 08:23:44")).toBeInTheDocument();
  });

  it.each([
    ["en", "10/5/2026, 5:23:44 PM"],
    ["pl", "5.10.2026, 17:23:44"],
  ])("the time zone does not depend on the language (%s)", async (locale, text) => {
    session({ date_format: "us", timezone: "Asia/Tokyo", date_format_explicit: false });
    await boot();
    render(inLanguage(locale));
    expect(screen.getByText(text)).toBeInTheDocument();
  });
});
