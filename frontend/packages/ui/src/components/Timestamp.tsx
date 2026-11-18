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

/** The day and clock time of a moment, the way a person says it: "Today 10:26 AM" for a moment
 *  from the current day, "Yesterday 18:04" for the one before, and the full formatted stamp for
 *  anything older. It honours the same display format (zone, 12/24 hour) as `Timestamp`. */
export function dayTime(date: Date, format: DisplayFormat | undefined, now = new Date()): string {
  const f = format ?? { dateFormat: "us" as DateFormat };
  const iso = f.dateFormat === "iso";
  const zone = f.timeZone && f.timeZone !== "browser" ? f.timeZone : undefined;
  let p: Record<string, string>;
  try {
    p = parts(date, iso, zone);
  } catch {
    p = parts(date, iso, undefined);
  }
  const clock = `${p.hour}:${p.minute}${iso ? "" : ` ${p.dayPeriod}`}`;
  // The comparison runs in the SAME zone as the label: "today" in Warsaw is not "today" in UTC.
  const key = (d: Date) => {
    const q = parts(d, iso, zone);
    return `${q.year}-${q.month}-${q.day}`;
  };
  const today = key(now);
  const yesterday = key(new Date(now.getTime() - 86_400_000));
  const day = key(date);
  if (day === today) return `Today ${clock}`;
  if (day === yesterday) return `Yesterday ${clock}`;
  return formatTimestamp(date, f).replace(/:\d\d(?= |$)/, "");
}

/** `dayTime` as an element. An unparseable value is shown as is. */
export function DayTime({ value }: { value: string }) {
  const format = useContext(DisplayFormatContext);
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return <time>{value}</time>;
  return <time dateTime={value}>{dayTime(d, format)}</time>;
}
