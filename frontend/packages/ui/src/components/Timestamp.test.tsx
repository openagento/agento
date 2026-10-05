import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DisplayFormatProvider, formatTimestamp, Timestamp } from "./Timestamp";

const MORNING = new Date("2026-10-05T08:23:44Z");
const EVENING = new Date("2026-10-05T20:05:09Z");
const MIDNIGHT = new Date("2026-01-02T00:00:07Z");

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

  it("a zone the browser does not know falls back to the browser's zone, it does not throw", () => {
    expect(formatTimestamp(MORNING, { dateFormat: "iso", timeZone: "Mars/Olympus" }))
      .toBe(formatTimestamp(MORNING, { dateFormat: "iso" }));
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

  it("with no provider keeps the browser's locale string", () => {
    render(<Timestamp value="2026-10-05T08:23:44Z" />);
    expect(screen.getByText(new Date("2026-10-05T08:23:44Z").toLocaleString())).toBeInTheDocument();
  });

  it("shows an unparseable value as is", () => {
    render(<DisplayFormatProvider value={{ dateFormat: "iso" }}><Timestamp value="soon" /></DisplayFormatProvider>);
    expect(screen.getByText("soon")).toBeInTheDocument();
  });
});
