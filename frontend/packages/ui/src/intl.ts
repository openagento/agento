// The one module that calls `Intl`. For a language tag it has no data for, `Intl` uses the
// default language of the browser, so two users of one panel would read two number formats.
// Each formatter below gets `formatLocales(tag)`, so that case is written as English.
// `numbers.test.ts` fails on an `Intl` call or a `toLocale…` call in any other module.
export const SOURCE_LOCALE = "en";

/** The languages a formatter may write `tag` in, in order: `tag`, then English. */
export const formatLocales = (tag: string): string[] => [tag, SOURCE_LOCALE];

export const numberFormat = (tag: string, options?: Intl.NumberFormatOptions) => new Intl.NumberFormat(formatLocales(tag), options);
export const dateTimeFormat = (tag: string, options?: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat(formatLocales(tag), options);
export const pluralRules = (tag: string) => new Intl.PluralRules(formatLocales(tag));
export const relativeTimeFormat = (tag: string, options?: Intl.RelativeTimeFormatOptions) =>
  new Intl.RelativeTimeFormat(formatLocales(tag), options);

/** The IANA zones the browser knows; none in a browser that cannot list them. */
export const timeZones = (): string[] => (typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : []);
