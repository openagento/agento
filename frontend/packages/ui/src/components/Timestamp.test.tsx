import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import { DayTime, dayTime, DisplayFormatProvider, formatTimestamp, Timestamp, WallTime } from "./Timestamp";

const MORNING = new Date("2026-10-05T08:23:44Z");
const EVENING = new Date("2026-10-05T20:05:09Z");
const MIDNIGHT = new Date("2026-01-02T00:00:07Z");
const UNKNOWN_HERE = "Mars/Olympus";

describe("formatTimestamp", () => {
  it("us: month/day/year, 12-hour", () => {
    expect(formatTimestamp(MORNING, { dateFormat: "us", timeZone: "UTC" })).toBe("10/5/2026, 8:23:44 AM");
    expect(formatTimestamp(EVENING, { dateFormat: "us", timeZone: "UTC" })).toBe("10/5/2026, 8:05:09 PM");
  });

  it("eu: day.month.year with no leading zeros, 12-hour", () => {
    expect(formatTimestamp(MORNING, { dateFormat: "eu", timeZone: "UTC" })).toBe("5.10.2026, 8:23:44 AM");
    expect(formatTimestamp(MIDNIGHT, { dateFormat: "eu", timeZone: "UTC" })).toBe("2.1.2026, 12:00:07 AM");
  });

  it("iso: year-month-day, 24-hour with leading zeros", () => {
    expect(formatTimestamp(MORNING, { dateFormat: "iso", timeZone: "UTC" })).toBe("2026-10-05 08:23:44");
    expect(formatTimestamp(EVENING, { dateFormat: "iso", timeZone: "UTC" })).toBe("2026-10-05 20:05:09");
    expect(formatTimestamp(MIDNIGHT, { dateFormat: "iso", timeZone: "UTC" })).toBe("2026-01-02 00:00:07");
  });

  it("shows the time in the given zone", () => {
    expect(formatTimestamp(MORNING, { dateFormat: "iso", timeZone: "Europe/Warsaw" })).toBe("2026-10-05 10:23:44");
  });

  it("browser (or no zone) uses the browser's zone", () => {
    const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const expected = formatTimestamp(MORNING, { dateFormat: "iso", timeZone: browserZone });
    expect(formatTimestamp(MORNING, { dateFormat: "iso", timeZone: "browser" })).toBe(expected);
    expect(formatTimestamp(MORNING, { dateFormat: "iso" })).toBe(expected);
  });

  it("a zone the browser does not know is written in UTC and the label says so: never the browser's zone in silence", () => {
    expect(formatTimestamp(MORNING, { dateFormat: "iso", timeZone: UNKNOWN_HERE })).toBe("2026-10-05 08:23:44 UTC");
    expect(formatTimestamp(MORNING, { dateFormat: "us", timeZone: UNKNOWN_HERE })).toBe("10/5/2026, 8:23:44 AM UTC");
    expect(formatTimestamp(MORNING, { dateFormat: "eu", timeZone: UNKNOWN_HERE })).toBe("5.10.2026, 8:23:44 AM UTC");
    const now = new Date("2026-10-05T12:00:00Z");
    expect(dayTime(MORNING, { dateFormat: "iso", timeZone: UNKNOWN_HERE }, now)).toBe("Today 08:23 UTC");
    expect(dayTime(new Date("2026-10-01T08:23:44Z"), { dateFormat: "iso", timeZone: UNKNOWN_HERE }, now)).toBe("2026-10-01 08:23 UTC");
  });
});

describe("a format with a language", () => {
  it("writes the date the way the language does, in the given zone", () => {
    expect(formatTimestamp(MORNING, { dateFormat: "us", timeZone: "UTC", language: "pl" })).toBe("5.10.2026, 08:23:44");
    expect(formatTimestamp(MORNING, { dateFormat: "us", timeZone: "Europe/Warsaw", language: "pl" })).toBe("5.10.2026, 10:23:44");
  });

  it("a zone the browser does not know is written in UTC and the label says so", () => {
    expect(formatTimestamp(MORNING, { dateFormat: "us", timeZone: UNKNOWN_HERE, language: "pl" })).toBe("5.10.2026, 08:23:44 UTC");
  });

  it("dayTime says the day in the given words and the clock in the language", () => {
    const now = new Date("2026-10-05T12:00:00Z");
    const polish = { dateFormat: "us" as const, timeZone: "UTC", language: "pl" };
    const t = (key: string, params?: Record<string, string | number>) => `${key}|${params?.time}`;
    expect(dayTime(MORNING, polish, now, t)).toBe("time.today|08:23");
    expect(dayTime(new Date("2026-10-04T20:05:09Z"), polish, now, t)).toBe("time.yesterday|20:05");
    expect(dayTime(new Date("2026-10-01T08:23:44Z"), polish, now, t)).toBe("1.10.2026, 08:23");
  });
});

describe("DayTime", () => {
  it("reads Today from the active language", () => {
    const now = new Date();
    render(
      <I18nProvider locale="pl" messages={{ ui: { "time.today": "Dziś {time}" } }}>
        <DisplayFormatProvider value={{ dateFormat: "iso", timeZone: "UTC" }}><DayTime value={now.toISOString()} /></DisplayFormatProvider>
      </I18nProvider>);
    expect(screen.getByText(/^Dziś \d\d:\d\d$/)).toBeInTheDocument();
  });
});

describe("Timestamp", () => {
  it("uses the provider's format", () => {
    render(<DisplayFormatProvider value={{ dateFormat: "eu", timeZone: "UTC" }}>
      <Timestamp value="2026-10-05T08:23:44Z" />
    </DisplayFormatProvider>);
    const time = screen.getByText("5.10.2026, 8:23:44 AM");
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("datetime", "2026-10-05T08:23:44Z");
  });

  it("with no provider writes the `us` format in the browser's zone, whatever the browser's language is", () => {
    render(<Timestamp value="2026-10-05T08:23:44Z" />);
    const label = formatTimestamp(new Date("2026-10-05T08:23:44Z"), { dateFormat: "us" });
    expect(label).toMatch(/^10\/\d\/2026, \d{1,2}:\d\d:44 [AP]M$/);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("shows an unparseable value as is", () => {
    render(<DisplayFormatProvider value={{ dateFormat: "iso" }}><Timestamp value="soon" /></DisplayFormatProvider>);
    expect(screen.getByText("soon")).toBeInTheDocument();
  });
});

describe("WallTime", () => {
  const AT = "2026-11-14T12:00:00Z";
  const LOCAL = "2026-11-14T09:00:00";

  it.each([
    ["iso", undefined, "2026-11-14 09:00:00"],
    ["us", undefined, "11/14/2026, 9:00:00 AM"],
    ["us", "pl", "14.11.2026, 09:00:00"],
  ] as const)("writes the server's wall time in the %s format (language %s), whatever the display zone is", (dateFormat, language, text) => {
    render(<DisplayFormatProvider value={{ dateFormat, language, timeZone: "Asia/Tokyo" }}><WallTime local={LOCAL} at={AT} /></DisplayFormatProvider>);
    const time = screen.getByText(text);
    expect(time).toHaveAttribute("datetime", AT);
  });

  it("asks the browser for no zone but UTC, so a zone only the server knows changes nothing", () => {
    const zoned = Intl.DateTimeFormat;
    const asked: (string | undefined)[] = [];
    vi.spyOn(Intl, "DateTimeFormat").mockImplementation(function recorded(tag?: string, options?: Intl.DateTimeFormatOptions) {
      asked.push(options?.timeZone);
      return new zoned(tag, options);
    } as typeof Intl.DateTimeFormat);
    render(<DisplayFormatProvider value={{ dateFormat: "iso" }}><WallTime local={LOCAL} at={AT} /></DisplayFormatProvider>);
    vi.restoreAllMocks();
    expect(screen.getByText("2026-11-14 09:00:00")).toBeInTheDocument();
    expect(new Set(asked)).toEqual(new Set(["UTC"]));
  });

  it("with no provider writes the `us` format, and shows an unparseable value as is", () => {
    render(<><WallTime local={LOCAL} at={AT} /><WallTime local="soon" at={AT} /></>);
    expect(screen.getByText("11/14/2026, 9:00:00 AM")).toBeInTheDocument();
    expect(screen.getByText("soon")).toBeInTheDocument();
  });
});
