import { createContext, useContext, type ReactNode } from "react";

export type DateFormat = "us" | "eu" | "iso";
/** `timeZone` is an IANA zone; absent or `"browser"` means the browser's zone. */
export interface DisplayFormat { dateFormat: DateFormat; timeZone?: string }

function parts(date: Date, iso: boolean, timeZone: string | undefined): Record<string, string> {
  // A fixed locale: the output depends only on the format and the zone, not the browser.
  const fmt = new Intl.DateTimeFormat("en-US", {
    year: "numeric", month: iso ? "2-digit" : "numeric", day: iso ? "2-digit" : "numeric",
    hour: iso ? "2-digit" : "numeric", minute: "2-digit", second: "2-digit",
    hourCycle: iso ? "h23" : "h12", timeZone,
  });
  return Object.fromEntries(fmt.formatToParts(date).map((p) => [p.type, p.value]));
}

export function formatTimestamp(date: Date, { dateFormat, timeZone }: DisplayFormat): string {
  const iso = dateFormat === "iso";
  const zone = timeZone && timeZone !== "browser" ? timeZone : undefined;
  let p: Record<string, string>;
  try {
    p = parts(date, iso, zone);
  } catch {
    p = parts(date, iso, undefined); // a zone this browser's ICU does not know
  }
  const time = `${p.hour}:${p.minute}:${p.second}`;
  if (iso) return `${p.year}-${p.month}-${p.day} ${time}`;
  const day = dateFormat === "eu" ? `${p.day}.${p.month}.${p.year}` : `${p.month}/${p.day}/${p.year}`;
  return `${day}, ${time} ${p.dayPeriod}`;
}

const DisplayFormatContext = createContext<DisplayFormat | undefined>(undefined);

export function DisplayFormatProvider({ value, children }: { value?: DisplayFormat; children: ReactNode }) {
  return <DisplayFormatContext.Provider value={value}>{children}</DisplayFormatContext.Provider>;
}

/** A machine-readable <time>, labelled in the provider's format (the browser's locale with no
 *  provider). An unparseable value is shown as is. */
export function Timestamp({ value }: { value: string }) {
  const format = useContext(DisplayFormatContext);
  const d = new Date(value);
  const label = Number.isNaN(d.getTime()) ? value : format ? formatTimestamp(d, format) : d.toLocaleString();
  return <time dateTime={value}>{label}</time>;
}
