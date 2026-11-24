import { describe, expect, it } from "vitest";
import type { EnablementItem } from "./enablementTree";
import { trifecta } from "./trifecta";

const tool = (name: string, labels: string[], requires: string | null = null): EnablementItem => ({
  name, path: `tools/${name}/is_enabled`, enabled: false, explicit_here: false, blocked_by: null, requires,
  catalog: { id: name.length, source: "module:m", labels, reviewed: false, revision: 0 },
});
const on = (...names: string[]) => new Set(names.map((n) => `tools/${n}/is_enabled`));

const READ = tool("mail_read", ["private-data"]);
const FETCH = tool("web_fetch", ["untrusted-content"]);
const SEND = tool("mail_send", ["external-communication"], "mail_read");
const ALL = tool("do_all", ["private-data", "untrusted-content", "external-communication"]);

describe("trifecta", () => {
  it.each([
    ["three tools", [READ, FETCH, SEND], on("mail_read", "web_fetch", "mail_send"), true],
    ["one missing", [READ, FETCH, SEND], on("mail_read", "web_fetch"), false],
    ["all on one tool", [ALL, READ], on("do_all"), true],
    ["requires master unchecked in draft", [READ, FETCH, SEND, tool("other_read", ["private-data"])],
      on("other_read", "web_fetch", "mail_send"), false],
    ["unknown master", [READ, FETCH, tool("x_send", ["external-communication"], "gone")],
      on("mail_read", "web_fetch", "x_send"), false],
    ["requires cycle", [READ, FETCH, tool("a", ["external-communication"], "b"), tool("b", [], "a")],
      on("mail_read", "web_fetch", "a", "b"), false],
    ["a tool with no catalog row", [READ, FETCH, { ...SEND, requires: null, catalog: null }],
      on("mail_read", "web_fetch", "mail_send"), false],
  ])("%s", (_name, items, checked, complete) => {
    expect(trifecta(items, checked).complete).toBe(complete);
  });

  it("names the tools of each category from the draft", () => {
    expect(trifecta([READ, FETCH, SEND, ALL], on("mail_read", "web_fetch", "mail_send", "do_all")).categories).toEqual({
      "private-data": ["mail_read", "do_all"],
      "untrusted-content": ["web_fetch", "do_all"],
      "external-communication": ["mail_send", "do_all"],
    });
  });
});
