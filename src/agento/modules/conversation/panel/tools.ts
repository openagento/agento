// One line per tool call (U2), from the canonical tool name and its input. The input is a JSON
// string (claude, pi, codex MCP), a raw command (codex shell) or absent: a reader without
// `conversation.run_details` gets no input, so the line is the verb alone.

import { numberFormat, useT, type Translate } from "@agento/ui";

const SHELL = new Set(["Bash", "bash", "shell", "local_shell", "exec_command"]);
const READ = new Set(["Read", "read"]);
const EDIT = new Set(["Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit"]);
const SEARCH = new Set(["Grep", "Glob", "grep", "glob", "find"]);

const line = (s: string, max = 80) => {
  const first = s.trim().split("\n", 1)[0];
  return first.length > max ? `${first.slice(0, max - 1)}…` : first;
};

function parse(input: unknown): { args: Record<string, unknown>; raw: string } {
  if (input && typeof input === "object" && !Array.isArray(input)) return { args: input as Record<string, unknown>, raw: "" };
  if (typeof input !== "string") return { args: {}, raw: "" };
  try {
    const v: unknown = JSON.parse(input);
    if (v && typeof v === "object" && !Array.isArray(v)) return { args: v as Record<string, unknown>, raw: "" };
    return { args: {}, raw: typeof v === "string" ? v : input };
  } catch {
    return { args: {}, raw: input };
  }
}

const pick = (args: Record<string, unknown>, ...keys: string[]) => {
  for (const k of keys) {
    const v = args[k];
    if (typeof v === "string" && v.trim()) return v;
    if (Array.isArray(v) && v.every((x) => typeof x === "string")) return v.join(" ");
  }
  return "";
};

type Verb = "ran" | "read" | "edited" | "searched" | "listed" | "fetched" | "web_searched";

/** What a call did: the verb and its object. No verb: an MCP tool, `what` is its name and first argument. */
function parts(name: string, input?: unknown): { verb: Verb | null; what: string } {
  const { args, raw } = parse(input);
  if (SHELL.has(name)) return { verb: "ran", what: pick(args, "command", "cmd") || raw };
  if (READ.has(name)) return { verb: "read", what: pick(args, "file_path", "path") };
  if (EDIT.has(name)) return { verb: "edited", what: pick(args, "file_path", "path", "notebook_path") };
  if (SEARCH.has(name)) return { verb: "searched", what: pick(args, "pattern", "query") };
  if (name === "ls") return { verb: "listed", what: pick(args, "path") };
  if (name === "WebFetch") return { verb: "fetched", what: pick(args, "url") };
  if (name === "WebSearch") return { verb: "web_searched", what: pick(args, "query") };
  // mcp__<server>__<tool> (claude) or a bare MCP tool name (codex, pi): the tool and its first
  // short string argument.
  const tool = name.startsWith("mcp__") ? name.split("__").slice(2).join("__") || name : name || "tool";
  const first = Object.values(args).find((v): v is string => typeof v === "string" && v.trim() !== "" && v.length <= 80);
  return { verb: null, what: first ? `${tool} ${line(first, 60)}` : tool };
}

/** Each verb done and while it runs, with its object and alone. */
function texts(t: Translate, verb: Verb, what: string): { done: string; doneAlone: string; live: string; liveAlone: string } {
  switch (verb) {
    case "ran": return { done: t("tool.ran", { what }), doneAlone: t("tool.ran_alone"), live: t("tool.running", { what }), liveAlone: t("tool.running_alone") };
    case "read": return { done: t("tool.read", { what }), doneAlone: t("tool.read_alone"), live: t("tool.reading", { what }), liveAlone: t("tool.reading_alone") };
    case "edited": return { done: t("tool.edited", { what }), doneAlone: t("tool.edited_alone"), live: t("tool.editing", { what }), liveAlone: t("tool.editing_alone") };
    case "searched": return { done: t("tool.searched", { what }), doneAlone: t("tool.searched_alone"), live: t("tool.searching", { what }), liveAlone: t("tool.searching_alone") };
    case "listed": return { done: t("tool.listed", { what }), doneAlone: t("tool.listed_alone"), live: t("tool.listing", { what }), liveAlone: t("tool.listing_alone") };
    case "fetched": return { done: t("tool.fetched", { what }), doneAlone: t("tool.fetched_alone"), live: t("tool.fetching", { what }), liveAlone: t("tool.fetching_alone") };
    case "web_searched": return { done: t("tool.web_searched", { what }), doneAlone: t("tool.web_searched_alone"), live: t("tool.web_searching", { what }), liveAlone: t("tool.web_searching_alone") };
  }
}

/** "Ran ls -1", "Read a.py", "jira_get_issue DEMO-1"; `live`, the same line while the call runs:
 *  "Running ls -1", "Reading a.py", "Running jira_get_issue DEMO-1". */
export function toolSummary(t: Translate, name: string, input?: unknown, live = false): string {
  const { verb, what } = parts(name, input);
  if (verb === null) return live ? t("tool.running", { what }) : what;
  const said = texts(t, verb, line(what));
  return what ? (live ? said.live : said.done) : (live ? said.liveAlone : said.doneAlone);
}

export type Summarize = (name: string, input?: unknown, live?: boolean) => string;

export function useToolSummary(): Summarize {
  const t = useT("conversation");
  return (name, input, live) => toolSummary(t, name, input, live);
}

/** "0.4 s", "12 s", "2 min 5 s"; nothing when either end is unknown. */
export function duration(locale: string, from?: string | null, to?: string | null): string | undefined {
  if (!from || !to) return undefined;
  const ms = Date.parse(to) - Date.parse(from);
  if (!Number.isFinite(ms) || ms < 0) return undefined;
  if (ms < 10_000) return `${numberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(ms / 1000)} s`;
  const s = Math.round(ms / 1000);
  const whole = numberFormat(locale);
  return s < 60 ? `${whole.format(s)} s` : `${whole.format(Math.floor(s / 60))} min ${whole.format(s % 60)} s`;
}
