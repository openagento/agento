import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { SOURCE_MESSAGES } from "../../../panel/src/generated/messages";
import { fileSize } from "./components/Chat";
import { PageHeader } from "./components/Headers";
import { formatTimestamp } from "./components/Timestamp";
import {
  format, I18nProvider, installSourceMessages, isMessage, PLURAL_CATEGORIES, translate, useDocumentTitle, useFormat, useLocale, useLookup, useT, withElements, type Messages,
} from "./i18n";

const JOBS = { one: "{count} job", other: "{count} jobs" };
const NBSP = String.fromCharCode(0xa0);

afterEach(() => installSourceMessages(SOURCE_MESSAGES));

describe("format", () => {
  const english = { messageLocale: "en", displayLocale: "en" };

  it("fills each hole with its param, as text", () => {
    expect(format("{a} and {b} and {a}", { a: "x", b: "<b>y</b>" }, english)).toBe("x and <b>y</b> and x");
  });

  it("keeps a hole that has no param", () => {
    expect(format("Hello, {name}", {}, english)).toBe("Hello, {name}");
  });

  it.each([
    ["en", 1, "1 job"], ["en", 2, "2 jobs"], ["en", 0, "0 jobs"],
  ])("picks the plural form by the rules of %s for %d", (messageLocale, count, text) => {
    expect(format(JOBS, { count }, { messageLocale, displayLocale: "en" })).toBe(text);
  });

  it.each([
    [1, "1 zadanie"], [3, "3 zadania"], [5, "5 zadań"], [22, "22 zadania"], [1.5, "1,5 zadania"],
  ])("picks the Polish form for %d", (count, text) => {
    const zadania = { one: "{count} zadanie", few: "{count} zadania", many: "{count} zadań", other: "{count} zadania" };
    expect(format(zadania, { count }, { messageLocale: "pl", displayLocale: "pl" })).toBe(text);
  });

  it("uses `other` when the picked form is missing, and when `count` is not a number", () => {
    expect(format({ other: "{count} things" }, { count: 1 }, english)).toBe("1 things");
    expect(format(JOBS, { count: "1" }, english)).toBe("1 jobs");
  });

  it("writes a number the way the displayed language writes it", () => {
    expect(format("{n} rows", { n: 12345.5 }, { messageLocale: "en", displayLocale: "pl" })).toBe(`12${NBSP}345,5 rows`);
    expect(format("{n} rows", { n: 12345.5 }, english)).toBe("12,345.5 rows");
  });
});

const within = (locale: string, messages: Messages, fallback?: Messages) =>
  ({ wrapper: ({ children }: { children: ReactNode }) => <I18nProvider locale={locale} messages={messages} fallback={fallback}>{children}</I18nProvider> });

describe("the lookup order", () => {
  it.each([
    ["the active language first", { demo: { k: "active" } }, { demo: { k: "served" } }, "active"],
    ["then the served English", {}, { demo: { k: "served" } }, "served"],
    ["then the bundled English", {}, {}, "bundled"],
  ])("%s", (_name, messages, fallback, text) => {
    installSourceMessages({ demo: { k: "bundled" } });
    expect(renderHook(() => useT("demo"), within("pl", messages, fallback)).result.current("k")).toBe(text);
  });

  it("shows the key when no layer holds it, and `useLookup` answers undefined", () => {
    const { result } = renderHook(() => ({ t: useT("demo"), lookup: useLookup() }), within("pl", {}));
    expect(result.current.t("missing.key")).toBe("missing.key");
    expect(result.current.lookup("demo", "missing.key")).toBeUndefined();
  });

  it("with no provider reads the bundled English", () => {
    const { result } = renderHook(() => ({ t: useT("ui"), locale: useLocale() }));
    expect(result.current.t("state.loading")).toBe("Loading…");
    expect(result.current.locale).toBe("en");
  });

  it("skips an entry that is not a message", () => {
    installSourceMessages({ demo: { k: "bundled" } });
    const broken = { demo: { k: 7 } } as unknown as Messages;
    expect(renderHook(() => useT("demo"), within("pl", broken)).result.current("k")).toBe("bundled");
  });
});

describe("a key the active language lacks (French, count 0.5)", () => {
  it.each([
    ["the served fallback", { demo: { jobs: JOBS } }, {}],
    ["the bundled English", {}, { demo: { jobs: JOBS } }],
  ])("%s picks its form by English rules and writes the number in French", (_name, fallback, bundled) => {
    installSourceMessages(bundled);
    const { result } = renderHook(() => useT("demo"), within("fr", { demo: {} }, fallback));
    expect(result.current("jobs", { count: 0.5 })).toBe("0,5 jobs");
  });

  it("a French message picks its form by French rules", () => {
    const french = { demo: { jobs: { one: "{count} tâche", other: "{count} tâches" } } };
    expect(renderHook(() => useT("demo"), within("fr", french)).result.current("jobs", { count: 0.5 })).toBe("0,5 tâche");
  });
});

describe("the hooks", () => {
  it("`t` keeps its identity while the catalog does not change", () => {
    const messages = { demo: { k: "v" } };
    const { result, rerender } = renderHook(() => useT("demo"), within("pl", messages));
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });

  it.each([["pl", `12${NBSP}345,5`], ["en", "12,345.5"]])("useFormat writes a number in %s", (locale, text) => {
    expect(renderHook(() => useFormat(), within(locale, {})).result.current.number(12345.5)).toBe(text);
  });

  it.each([
    ["en", "1,234,567", "42%", "2m 05s", "1h 01m"],
    ["pl", `1${NBSP}234${NBSP}567`, "42%", "2m 05s", "1h 01m"],
    ["fa", "۱٬۲۳۴٬۵۶۷", "۴۲٪", "۲m ۰۵s", "۱h ۰۱m"],
    ["ar-EG", "١٬٢٣٤٬٥٦٧", new Intl.NumberFormat("ar-EG", { style: "percent" }).format(0.42), "٢m ٠٥s", "١h ٠١m"],
  ])("useFormat writes a count, a share and every part of a duration with the digits of %s", (locale, count, share, minutes, hours) => {
    const { number, percent, duration } = renderHook(() => useFormat(), within(locale, {})).result.current;
    expect([number(1234567), percent(42), duration(125), duration(3660)]).toEqual([count, share, minutes, hours]);
  });

  it("useDocumentTitle names the tab, and no title leaves it", () => {
    document.title = "before";
    const { rerender } = renderHook(({ title }: { title?: string }) => useDocumentTitle(title), { initialProps: {} });
    expect(document.title).toBe("before");
    rerender({ title: "Jobs" });
    expect(document.title).toBe("Jobs · Agento");
  });

  it("PageHeader names the tab with its title", () => {
    render(<PageHeader title="Zadania" />);
    expect(screen.getByRole("heading", { name: "Zadania" })).toBeInTheDocument();
    expect(document.title).toBe("Zadania · Agento");
  });
});

describe("an installed language that the browser has no data for (qaa)", () => {
  // `Intl` writes such a tag with the default language of the browser. So the same panel would
  // show `1234,5` in a Polish browser and `1,234.5` in an English one. English is the rule.
  const QAA: Messages = { demo: { title: "Qaa title", size: "qaa {n}", jobs: { one: "qaa one {count}", few: "qaa few {count}", other: "qaa other {count}" } } };
  const MOMENT = new Date("2026-10-05T08:23:44Z");

  it("shows the texts of its own catalog", () => {
    const t = renderHook(() => useT("demo"), within("qaa", QAA)).result.current;
    expect(t("title")).toBe("Qaa title");
  });

  it("writes a number, a share, a duration, a plural form, a date and a file size as English does", () => {
    const hooks = (locale: string) => renderHook(() => ({ t: useT("demo"), ...useFormat() }), within(locale, QAA)).result.current;
    const written = (locale: string) => {
      const { t, number, percent, duration } = hooks(locale);
      return [
        t("size", { n: 1234.5 }), t("jobs", { count: 2 }), t("jobs", { count: 1 }), number(1234567.5), percent(42), duration(125),
        formatTimestamp(MOMENT, { dateFormat: "us", timeZone: "UTC", language: locale }), fileSize(1536, locale),
      ];
    };
    expect(written("qaa")).toEqual(written("en"));
    expect(written("qaa").slice(0, 6)).toEqual(["qaa 1,234.5", "qaa other 2", "qaa one 1", "1,234,567.5", "42%", "2m 05s"]);
  });
});

describe("a message of a wrong shape", () => {
  it.each([
    ["a plural form that is a number", { other: "{count} jobs", few: 7 }],
    ["a plural category that no language has", { other: "{count} jobs", several: "x" }],
    ["a plural with no `other`", { one: "{count} job" }],
    ["a number", 7],
    ["an array", ["x"]],
    ["null", null],
  ])("%s is not a message: the lookup goes to the next layer and never throws", (_name, message) => {
    expect(isMessage(message)).toBe(false);
    const catalog = { locale: "pl", messages: { demo: { k: message } } as unknown as Messages, fallback: { demo: { k: "English" } } };
    expect(translate(catalog, "demo", "k", { count: 3 })).toBe("English");
  });

  it("a plural message has the categories of the server's PLURAL_CATEGORIES", () => {
    const python = readFileSync(join(__dirname, "../../../../src/agento/framework/i18n/__init__.py"), "utf8");
    const listed = /^PLURAL_CATEGORIES = frozenset\(\{(.+)\}\)$/m.exec(python)![1];
    expect(listed.split(",").map((name) => name.trim().replace(/"/g, "")).sort()).toEqual([...PLURAL_CATEGORIES].sort());
  });

  it.each([["a text", "x"], ["a plural", { one: "a", other: "b" }], ["a plural with `other` only", { other: "b" }]])("%s is a message", (_name, message) => {
    expect(isMessage(message)).toBe(true);
  });
});

describe("withElements", () => {
  it("puts an element in the hole it names, where the sentence of the language has it", () => {
    const { container } = render(<p>{withElements("Zaktualizowano {time} przez {who}.", { who: <b>ann</b>, time: <time>teraz</time> })}</p>);
    expect(container.innerHTML).toBe("<p>Zaktualizowano <time>teraz</time> przez <b>ann</b>.</p>");
  });

  it("keeps a hole the caller gives no element for", () => {
    const { container } = render(<p>{withElements("{a} of {b}", { a: <i>1</i> })}</p>);
    expect(container.innerHTML).toBe("<p><i>1</i> of {b}</p>");
  });
});
