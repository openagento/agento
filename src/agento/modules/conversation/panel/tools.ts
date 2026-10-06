// One line per tool call (U2), from the canonical tool name and its input. The input is a JSON
// string (claude, pi, codex MCP), a raw command (codex shell) or absent: a reader without
// `conversation.run_details` gets no input, so the line is the verb alone.

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

const said = (verb: string, what: string, fallback: string) => (what ? `${verb} ${line(what)}` : fallback);

export function toolSummary(name: string, input?: unknown): string {
  const { args, raw } = parse(input);
  if (SHELL.has(name)) return said("Ran", pick(args, "command", "cmd") || raw, "Ran a shell command");
  if (READ.has(name)) return said("Read", pick(args, "file_path", "path"), "Read a file");
  if (EDIT.has(name)) return said("Edited", pick(args, "file_path", "path", "notebook_path"), "Edited a file");
  if (SEARCH.has(name)) return said("Searched", pick(args, "pattern", "query"), "Searched files");
  if (name === "ls") return said("Listed", pick(args, "path"), "Listed files");
  if (name === "WebFetch") return said("Fetched", pick(args, "url"), "Fetched a page");
  if (name === "WebSearch") return said("Searched the web for", pick(args, "query"), "Searched the web");
  // mcp__<server>__<tool> (claude) or a bare MCP tool name (codex, pi): the tool and its first
  // short string argument.
  const tool = name.startsWith("mcp__") ? name.split("__").slice(2).join("__") || name : name || "tool";
  const first = Object.values(args).find((v): v is string => typeof v === "string" && v.trim() !== "" && v.length <= 80);
  return first ? `${tool} ${line(first, 60)}` : tool;
}

const LIVE: Record<string, string> = {
  Ran: "Running", Read: "Reading", Edited: "Editing", Searched: "Searching", Listed: "Listing", Fetched: "Fetching",
};

/** The same line while the call runs: "Running ls -1", "Reading a.py", "Running jira_get_issue DEMO-1". */
export function liveSummary(summary: string): string {
  const [verb, ...rest] = summary.split(" ");
  return LIVE[verb] ? [LIVE[verb], ...rest].join(" ") : `Running ${summary}`;
}

/** "0.4 s", "12 s", "2 min 5 s"; nothing when either end is unknown. */
export function duration(from?: string | null, to?: string | null): string | undefined {
  if (!from || !to) return undefined;
  const ms = Date.parse(to) - Date.parse(from);
  if (!Number.isFinite(ms) || ms < 0) return undefined;
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)} s`;
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}
