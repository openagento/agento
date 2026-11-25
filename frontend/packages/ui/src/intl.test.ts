import { describe, expect, it } from "vitest";
import { dateTimeFormat, formatLocales, numberFormat, pluralRules, relativeTimeFormat } from "./intl";

// `qaa` is a well-formed tag that no browser has data for. The resolved language says which data
// a formatter uses: with the tag alone it is the default language of the runtime (`en-US`, `pl`),
// which the test must not depend on.
describe("the formatters", () => {
  const MAKERS = { numberFormat, dateTimeFormat, pluralRules, relativeTimeFormat };

  it.each(Object.entries(MAKERS))("%s writes a language with no data as English, whatever the default language of the runtime is", (_name, make) => {
    expect(make("qaa").resolvedOptions().locale).toBe("en");
  });

  it.each(Object.entries(MAKERS))("%s writes a language with data in that language", (_name, make) => {
    expect(make("pl").resolvedOptions().locale).toBe("pl");
    expect(make("de-CH").resolvedOptions().locale).toMatch(/^de/);
  });

  it("the list is the tag, then English", () => {
    expect(formatLocales("qaa")).toEqual(["qaa", "en"]);
  });
});
