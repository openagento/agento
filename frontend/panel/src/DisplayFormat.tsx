import { useMemo, type ReactNode } from "react";
import { useDisplay } from "@agento/api";
import { DisplayFormatProvider, SOURCE_LOCALE, useLocale } from "@agento/ui";

const SHIPPED_DISPLAY = { date_format: "us", timezone: "browser", date_format_explicit: false } as const;

/** Every Timestamp in the panel uses the session's display settings (GET /api/session). A date
 *  format an operator set wins. With none set, English keeps the shipped format and any other
 *  language writes a date its own way. The time zone never depends on the language. A session
 *  with no display settings (the `admin` module is off) uses the shipped ones. */
export function SessionDisplayFormat({ children }: { children: ReactNode }) {
  const d = useDisplay() ?? SHIPPED_DISPLAY;
  const locale = useLocale();
  const value = useMemo(() => {
    const ownFormat = d.date_format_explicit || locale === SOURCE_LOCALE;
    return { dateFormat: d.date_format, timeZone: d.timezone, language: ownFormat ? undefined : locale };
  }, [d, locale]);
  return <DisplayFormatProvider value={value}>{children}</DisplayFormatProvider>;
}
