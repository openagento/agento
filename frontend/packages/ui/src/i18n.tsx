import { createContext, Fragment, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";
import { numberFormat, pluralRules, SOURCE_LOCALE } from "./intl";

export { SOURCE_LOCALE };

export const PLURAL_CATEGORIES = ["zero", "one", "two", "few", "many", "other"] as const;
export type PluralMessage = { other: string } & Partial<Record<(typeof PLURAL_CATEGORIES)[number], string>>;
export type Message = string | PluralMessage;
export type Messages = Record<string, Record<string, Message>>;
export type MessageParams = Record<string, string | number>;

/** What one language serves: its own `messages`, and the English `fallback` for the keys it lacks. */
export interface Catalog { locale: string; messages: Messages; fallback: Messages }

const NO_MESSAGES: Messages = {};
const SOURCE_ONLY: Catalog = { locale: SOURCE_LOCALE, messages: NO_MESSAGES, fallback: NO_MESSAGES };

let sourceMessages: Messages = NO_MESSAGES;

/** The English texts built into the bundle. Call it once, before the first render. */
export function installSourceMessages(en: Messages): void {
  sourceMessages = en;
}

const HOLE = /\{(\w+)\}/g;

function pluralForm(message: PluralMessage, count: unknown, messageLocale: string): string {
  if (typeof count !== "number") return message.other;
  return message[pluralRules(messageLocale).select(count)] ?? message.other;
}

/** `{param}` holes are filled as text. A plural message picks its form by the rules of the language
 *  it is written in; a number is written the way the displayed language writes it. */
export function format(message: Message, params: MessageParams = {}, { messageLocale, displayLocale }: {
  messageLocale: string; displayLocale: string;
}): string {
  const text = typeof message === "string" ? message : pluralForm(message, params.count, messageLocale);
  return text.replace(HOLE, (hole, name: string) => {
    const value = params[name];
    if (value === undefined) return hole;
    return typeof value === "number" ? numberFormat(displayLocale).format(value) : value;
  });
}

/** A translated sentence with an element in each `{hole}` the caller names:
 *  `withElements(t("artifact.stale"), { version: <code>v2</code> })`. Leave those params out of `t`. */
export function withElements(sentence: string, elements: Record<string, ReactNode>): ReactNode[] {
  return sentence.split(new RegExp(HOLE.source)).map((part, i) => <Fragment key={i}>{i % 2 ? elements[part] ?? `{${part}}` : part}</Fragment>);
}

/** The server's rule for a catalog entry (`message_error`): a text, or a plural object with
 *  `other`, whose keys are plural categories and whose values are texts. */
export function isMessage(m: unknown): m is Message {
  if (typeof m === "string") return true;
  if (typeof m !== "object" || m === null || Array.isArray(m) || !("other" in m)) return false;
  for (const [category, text] of Object.entries(m)) {
    if (typeof text !== "string" || !(PLURAL_CATEGORIES as readonly string[]).includes(category)) return false;
  }
  return true;
}

/** The text of `namespace:key`: the active language, then the served English, then the bundled
 *  English. `undefined` when no layer holds the key. */
export function translate(catalog: Catalog, namespace: string, key: string, params?: MessageParams): string | undefined {
  const active = catalog.messages[namespace]?.[key];
  if (isMessage(active)) return format(active, params, { messageLocale: catalog.locale, displayLocale: catalog.locale });
  const english = catalog.fallback[namespace]?.[key] ?? sourceMessages[namespace]?.[key];
  if (isMessage(english)) return format(english, params, { messageLocale: SOURCE_LOCALE, displayLocale: catalog.locale });
  return undefined;
}

const I18nContext = createContext<Catalog>(SOURCE_ONLY);

export function I18nProvider({ locale, messages, fallback = NO_MESSAGES, children }: {
  locale: string; messages: Messages; fallback?: Messages; children: ReactNode;
}) {
  const catalog = useMemo(() => ({ locale, messages, fallback }), [locale, messages, fallback]);
  return <I18nContext.Provider value={catalog}>{children}</I18nContext.Provider>;
}

export type Translate = (key: string, params?: MessageParams) => string;
export type Lookup = (namespace: string, key: string, params?: MessageParams) => string | undefined;

/** `t(key, params?)` for one namespace. A key that no layer holds is shown as the key. */
export function useT(namespace: string): Translate {
  const catalog = useContext(I18nContext);
  return useCallback((key, params) => translate(catalog, namespace, key, params) ?? key, [catalog, namespace]);
}

/** A lookup that answers `undefined` on a miss, for a text whose source is not in a catalog
 *  (a config label from `system.json`, an API error code): the caller gives the other text. */
export function useLookup(): Lookup {
  const catalog = useContext(I18nContext);
  return useCallback((namespace, key, params) => translate(catalog, namespace, key, params), [catalog]);
}

export function useLocale(): string {
  return useContext(I18nContext).locale;
}

export interface Formatters {
  number: (n: number) => string;
  /** `share` is out of 100: `percent(42)` is "42%". */
  percent: (share: number) => string;
  duration: (seconds: number | null) => string;
}

export function useFormat(): Formatters {
  const locale = useLocale();
  const t = useT("ui");
  return useMemo(() => {
    const numbers = numberFormat(locale);
    const shares = numberFormat(locale, { style: "percent", maximumFractionDigits: 0 });
    const padded = numberFormat(locale, { minimumIntegerDigits: 2 });
    const twoDigits = (n: number) => padded.format(n);
    return {
      number: (n) => numbers.format(n),
      percent: (share) => shares.format(share / 100),
      duration: (seconds) => {
        if (seconds === null) return "—";
        if (seconds < 60) return t("duration.seconds", { seconds });
        if (seconds < 3600) return t("duration.minutes", { minutes: Math.floor(seconds / 60), seconds: twoDigits(seconds % 60) });
        return t("duration.hours", { hours: Math.floor(seconds / 3600), minutes: twoDigits(Math.floor(seconds / 60) % 60) });
      },
    };
  }, [locale, t]);
}

export const PRODUCT_NAME = "Agento";

/** Sets the browser tab title to `<title> · Agento`. No title: the tab keeps what it shows. */
export function useDocumentTitle(title: string | undefined): void {
  useEffect(() => {
    if (title) document.title = `${title} · ${PRODUCT_NAME}`;
  }, [title]);
}
