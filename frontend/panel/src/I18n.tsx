import { useEffect, useSyncExternalStore, type ReactNode } from "react";
import { I18nProvider } from "@agento/ui";
import { getCatalog, subscribeCatalog } from "./catalogStore";

/** The language of a screen that shows its own texts, not the session's (the sign-in screen).
 *  While one is mounted it is the page language; `html lang` has this one writer. */
let screenLanguage: string | null = null;
const writePageLanguage = () => { document.documentElement.lang = screenLanguage ?? getCatalog().locale; };

export function useScreenLanguage(locale: string | null): void {
  useEffect(() => {
    if (locale === null) return undefined;
    screenLanguage = locale;
    writePageLanguage();
    return () => { screenLanguage = null; writePageLanguage(); };
  }, [locale]);
}

/** Every text in the panel reads the held catalog. Nothing below is keyed on the language, so a
 *  language change re-renders the screens and remounts none (a draft and a stream stay). */
export function SessionI18n({ children }: { children: ReactNode }) {
  const catalog = useSyncExternalStore(subscribeCatalog, getCatalog, getCatalog);
  useEffect(writePageLanguage, [catalog.locale]);
  return <I18nProvider locale={catalog.locale} messages={catalog.messages} fallback={catalog.fallback}>{children}</I18nProvider>;
}
