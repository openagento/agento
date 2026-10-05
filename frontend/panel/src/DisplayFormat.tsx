import { useMemo, type ReactNode } from "react";
import { useDisplay } from "@agento/api";
import { DisplayFormatProvider } from "@agento/ui";

/** Every Timestamp in the panel uses the session's display settings (GET /api/session). */
export function SessionDisplayFormat({ children }: { children: ReactNode }) {
  const d = useDisplay();
  const value = useMemo(() => (d ? { dateFormat: d.date_format, timeZone: d.timezone } : undefined), [d]);
  return <DisplayFormatProvider value={value}>{children}</DisplayFormatProvider>;
}
