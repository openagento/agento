import { describe, expect, it } from "vitest";
import { duration, liveSummary, toolSummary } from "./tools";

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
  ])("%s %s → %s", (name, input, out) => expect(toolSummary(name, input)).toBe(out));

  it("without input (no run details) says the verb alone", () => {
    expect(toolSummary("Bash")).toBe("Ran a shell command");
    expect(toolSummary("Read")).toBe("Read a file");
    expect(toolSummary("mcp__toolbox__jira_get_issue")).toBe("jira_get_issue");
  });

  it("cuts a long command to one short line", () => {
    const s = toolSummary("Bash", JSON.stringify({ command: "x".repeat(200) }));
    expect(s.length).toBeLessThanOrEqual(84);
    expect(s.endsWith("…")).toBe(true);
  });
});

it("liveSummary says the line in the present tense", () => {
  expect(liveSummary("Ran ls")).toBe("Running ls");
  expect(liveSummary("Read a file")).toBe("Reading a file");
  expect(liveSummary("jira_get_issue DEMO-1")).toBe("Running jira_get_issue DEMO-1");
});

describe("duration", () => {
  it("formats the time between start and end", () => {
    expect(duration("2026-10-06T10:00:00Z", "2026-10-06T10:00:01.400Z")).toBe("1.4 s");
    expect(duration("2026-10-06T10:00:00Z", "2026-10-06T10:02:05Z")).toBe("2 min 5 s");
    expect(duration(null, "2026-10-06T10:00:00Z")).toBeUndefined();
  });
});
