import { createContext, useContext, type ReactNode } from "react";
import { SOURCE_LOCALE, translate, useT, type Translate } from "../i18n";
import { dateTimeFormat } from "../intl";

export type DateFormat = "us" | "eu" | "iso";
/** `timeZone` is an IANA zone; absent or `"browser"` means the browser's zone.
 *  `language` is a BCP 47 tag: when present, a date is written the way that language writes it
 *  and `dateFormat` is not used. */
export interface DisplayFormat { dateFormat: DateFormat; timeZone?: string; language?: string }

const UTC = "UTC";
const FIXED_LOCALE = "en-US";
const NO_FORMAT: DisplayFormat = { dateFormat: "us" };
const knownZones = new Map<string, boolean>();
function browserKnows(zone: string): boolean {
  if (!knownZones.has(zone)) {
    try {
      dateTimeFormat(FIXED_LOCALE, { timeZone: zone });
      knownZones.set(zone, true);
    } catch {
      knownZones.set(zone, false);
    }
  }
  return knownZones.get(zone)!;
}

/** The zone a moment is written in. The configured zone is the server's choice, and the
 *  browser's zone data can lack it: the moment is then written in UTC and `named` says that the
 *  label must name UTC. It is never written in the browser's zone as if it were the configured one. */
function zoneOf(timeZone: string | undefined): { zone: string | undefined; named: boolean } {
  if (!timeZone || timeZone === "browser") return { zone: undefined, named: false };
  return browserKnows(timeZone) ? { zone: timeZone, named: false } : { zone: UTC, named: true };
}
const withZoneName = (label: string, named: boolean) => (named ? `${label} ${UTC}` : label);

const CLOCK: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };
const DAY_AND_CLOCK: Intl.DateTimeFormatOptions = { year: "numeric", month: "numeric", day: "numeric", ...CLOCK };
const DAY_AND_SECONDS: Intl.DateTimeFormatOptions = { ...DAY_AND_CLOCK, second: "2-digit" };

function inLanguage(date: Date, language: string, timeZone: string | undefined, fields: Intl.DateTimeFormatOptions): string {
  const { zone, named } = zoneOf(timeZone);
  return withZoneName(dateTimeFormat(language, { ...fields, timeZone: zone }).format(date), named);
}

const sourceText: Translate = (key, params) =>
  translate({ locale: SOURCE_LOCALE, messages: {}, fallback: {} }, "ui", key, params) ?? key;

function parts(date: Date, iso: boolean, timeZone: string | undefined): Record<string, string> {
  // A fixed locale: the output depends only on the format and the zone, not the browser.
  const fmt = dateTimeFormat(FIXED_LOCALE, {
    year: "numeric", month: iso ? "2-digit" : "numeric", day: iso ? "2-digit" : "numeric",
    hour: iso ? "2-digit" : "numeric", minute: "2-digit", second: "2-digit",
    hourCycle: iso ? "h23" : "h12", timeZone,
  });
  return Object.fromEntries(fmt.formatToParts(date).map((p) => [p.type, p.value]));
}

export function formatTimestamp(date: Date, { dateFormat, timeZone, language }: DisplayFormat): string {
  if (language) return inLanguage(date, language, timeZone, DAY_AND_SECONDS);
  const iso = dateFormat === "iso";
  const { zone, named } = zoneOf(timeZone);
  const p = parts(date, iso, zone);
  const time = `${p.hour}:${p.minute}:${p.second}`;
  if (iso) return withZoneName(`${p.year}-${p.month}-${p.day} ${time}`, named);
  const day = dateFormat === "eu" ? `${p.day}.${p.month}.${p.year}` : `${p.month}/${p.day}/${p.year}`;
  return withZoneName(`${day}, ${time} ${p.dayPeriod}`, named);
}

const DisplayFormatContext = createContext<DisplayFormat | undefined>(undefined);

export function DisplayFormatProvider({ value, children }: { value?: DisplayFormat; children: ReactNode }) {
  return <DisplayFormatContext.Provider value={value}>{children}</DisplayFormatContext.Provider>;
}

/** A machine-readable <time>, labelled in the provider's format (the `us` format in the browser's
 *  zone with no provider). An unparseable value is shown as is. */
export function Timestamp({ value }: { value: string }) {
  const format = useContext(DisplayFormatContext) ?? NO_FORMAT;
  const d = new Date(value);
  const label = Number.isNaN(d.getTime()) ? value : formatTimestamp(d, format);
  return <time dateTime={value}>{label}</time>;
}

/** A wall time that the server computed in a zone of its own choice (`local`, with no offset),
 *  for the moment `at`. The label is `local` in the provider's date format and language, with
 *  no zone conversion here: the browser's zone data can differ from the server's. */
export function WallTime({ local, at }: { local: string; at: string }) {
  const format = useContext(DisplayFormatContext) ?? NO_FORMAT;
  const asUtc = new Date(`${local}Z`);
  const label = Number.isNaN(asUtc.getTime()) ? local : formatTimestamp(asUtc, { ...format, timeZone: UTC });
  return <time dateTime={at}>{label}</time>;
}

/** The day and clock time of a moment, the way a person says it: "Today 10:26 AM" for a moment
 *  from the current day, "Yesterday 18:04" for the one before, and the full formatted stamp for
 *  anything older. It honours the same display format (zone, 12/24 hour) as `Timestamp`. */
export function dayTime(date: Date, format: DisplayFormat | undefined, now = new Date(), t: Translate = sourceText): string {
  const f = format ?? NO_FORMAT;
  const iso = f.dateFormat === "iso";
  const { zone, named } = zoneOf(f.timeZone);
  const p = parts(date, iso, zone);
  const clock = f.language
    ? inLanguage(date, f.language, f.timeZone, CLOCK)
    : withZoneName(`${p.hour}:${p.minute}${iso ? "" : ` ${p.dayPeriod}`}`, named);
  // The comparison runs in the SAME zone as the label: "today" in Warsaw is not "today" in UTC.
  const key = (d: Date) => {
    const q = parts(d, iso, zone);
    return `${q.year}-${q.month}-${q.day}`;
  };
  const today = key(now);
  const yesterday = key(new Date(now.getTime() - 86_400_000));
  const day = key(date);
  if (day === today) return t("time.today", { time: clock });
  if (day === yesterday) return t("time.yesterday", { time: clock });
  if (f.language) return inLanguage(date, f.language, f.timeZone, DAY_AND_CLOCK);
  return formatTimestamp(date, f).replace(/:\d\d(?= |$)/, "");
}

/** `dayTime` as an element. An unparseable value is shown as is. */
export function DayTime({ value }: { value: string }) {
  const format = useContext(DisplayFormatContext);
  const t = useT("ui");
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return <time>{value}</time>;
  return <time dateTime={value}>{dayTime(d, format, new Date(), t)}</time>;
}
