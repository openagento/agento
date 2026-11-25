import { describe, expect, it } from "vitest";
import { translate, type Catalog, type Translate } from "@agento/ui";
import { builtInMessages } from "../../../../../frontend/panel/src/routes/admin/testing";
import { duration, toolSummary } from "./tools";

const inLanguage = (tag: string): Translate => {
  const catalog: Catalog = { locale: tag, messages: builtInMessages(tag), fallback: {} };
  return (key, params) => translate(catalog, "conversation", key, params) ?? key;
};
const en = inLanguage("en");
const pl = inLanguage("pl");

describe("toolSummary", () => {
  it.each([
    ["Bash", JSON.stringify({ command: "ls -1 | head -5\necho done" }), "Ran ls -1 | head -5"],
    ["shell", "git status", "Ran git status"], // codex: the raw command
    ["bash", JSON.stringify({ command: "pwd" }), "Ran pwd"], // pi
    ["Read", JSON.stringify({ file_path: "/w/README.md" }), "Read /w/README.md"],
    ["Edit", JSON.stringify({ file_path: "a.py", old_string: "x" }), "Edited a.py"],
    ["Grep", JSON.stringify({ pattern: "TODO" }), "Searched TODO"],
    ["WebFetch", JSON.stringify({ url: "https://x.test" }), "Fetched https://x.test"],
    ["mcp__toolbox__jira_get_issue", JSON.stringify({ key: "DEMO-1" }), "jira_get_issue DEMO-1"],
    ["jira_get_issue", JSON.stringify({ key: "DEMO-1" }), "jira_get_issue DEMO-1"], // codex MCP
    ["Odd", "{not json", "Odd"],
  ])("%s %s → %s", (name, input, out) => expect(toolSummary(en, name, input)).toBe(out));

  it("without input (no run details) says the verb alone", () => {
    expect(toolSummary(en, "Bash")).toBe("Ran a shell command");
    expect(toolSummary(en, "Read")).toBe("Read a file");
    expect(toolSummary(en, "mcp__toolbox__jira_get_issue")).toBe("jira_get_issue");
  });

  it("cuts a long command to one short line", () => {
    const s = toolSummary(en, "Bash", JSON.stringify({ command: "x".repeat(200) }));
    expect(s.length).toBeLessThanOrEqual(84);
    expect(s.endsWith("…")).toBe(true);
  });

  it("says the line in the present tense while the call runs", () => {
    expect(toolSummary(en, "Bash", "ls", true)).toBe("Running ls");
    expect(toolSummary(en, "Read", undefined, true)).toBe("Reading a file");
    expect(toolSummary(en, "WebSearch", JSON.stringify({ query: "rrule" }), true)).toBe("Searching the web for rrule");
    expect(toolSummary(en, "jira_get_issue", JSON.stringify({ key: "DEMO-1" }), true)).toBe("Running jira_get_issue DEMO-1");
  });

  const VERBS: [string, string][] = [
    ["Bash", "command"], ["Read", "file_path"], ["Edit", "file_path"], ["Grep", "pattern"], ["ls", "path"], ["WebFetch", "url"],
    ["WebSearch", "query"],
  ];
  it.each(VERBS)("%s has its four lines in English and in Polish", (name, arg) => {
    const lines = (t: Translate) => [false, true].flatMap((live) =>
      [toolSummary(t, name, JSON.stringify({ [arg]: "x.py" }), live), toolSummary(t, name, undefined, live)]);
    const [english, polish] = [lines(en), lines(pl)];
    expect(new Set([...english, ...polish]).size).toBe(8);
    for (const line of [...english, ...polish]) expect(line).not.toMatch(/^tool\./);
    expect(english[0]).toContain("x.py");
    expect(polish[2]).toContain("x.py");
  });

  it("says a running MCP tool in Polish and keeps its name", () => {
    expect(toolSummary(pl, "jira_get_issue", JSON.stringify({ key: "DEMO-1" }), true)).toBe("Uruchamianie jira_get_issue DEMO-1");
    expect(toolSummary(pl, "Bash", "ls")).toBe("Uruchomiono ls");
  });
});

describe("duration", () => {
  it("formats the time between start and end", () => {
    expect(duration("en", "2026-10-06T10:00:00Z", "2026-10-06T10:00:01.400Z")).toBe("1.4 s");
    expect(duration("en", "2026-10-06T10:00:00Z", "2026-10-06T10:02:05Z")).toBe("2 min 5 s");
    expect(duration("en", null, "2026-10-06T10:00:00Z")).toBeUndefined();
  });

  it("writes the decimal the way the language does", () => {
    expect(duration("en", "2026-10-06T10:00:00Z", "2026-10-06T10:00:02Z")).toBe("2.0 s");
    expect(duration("pl", "2026-10-06T10:00:00Z", "2026-10-06T10:00:01.400Z")).toBe("1,4 s");
  });

  it("writes whole seconds and minutes with the digits of the language", () => {
    expect(duration("fa", "2026-10-06T10:00:00Z", "2026-10-06T10:00:12Z")).toBe("۱۲ s");
    expect(duration("fa", "2026-10-06T10:00:00Z", "2026-10-06T10:02:05Z")).toBe("۲ min ۵ s");
  });
});
