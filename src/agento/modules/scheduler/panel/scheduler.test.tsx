import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it } from "vitest";
import { ApiError, queryClient } from "@agento/api";
import { renderAt, stubApi, teardown } from "../../../../../frontend/panel/src/routes/admin/testing";
import { CODES, message } from "./api";
import { buildRrule, type Spec } from "./RecurrenceDialog";
import SchedulesPage from "./SchedulesPage";
import SchedulePage from "./ScheduleForm";

const B = "/api/scheduler";
const CAN = { read: true, write: true, share: true, delete: true, change_owner: true };
const row = (id: number, title: string, extra: Record<string, unknown> = {}) => ({
  id, source: "scheduler", source_ref: null, title, prompt: "Say hi", agent_view_id: 3, agent_view_code: "dev",
  owner_user_id: 1, owner_username: "ann", owner_agent_view_id: null, rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
  recurrence_label: "Every day at 9:00", starts_at: "2026-11-01T09:00:00", timezone: "Europe/Warsaw", harness: null,
  model: null, enabled: true, next_fire_at: "2026-11-14T08:00:00Z", last_fire_at: null, last_error: null,
  created_at: "2026-11-01T08:00:00Z", last_run_status: "SUCCESS", conversation_id: 9, can: CAN, members: [], ...extra,
});
const JIRA = row(5, "PROJ-7 weekly", {
  source: "jira_periodic", source_ref: "PROJ-7", owner_user_id: null, owner_username: null, owner_agent_view_id: 3,
  rrule: "FREQ=WEEKLY;BYDAY=MO;BYHOUR=7;BYMINUTE=30", recurrence_label: "FREQ=WEEKLY;BYDAY=MO;BYHOUR=7;BYMINUTE=30",
  conversation_id: null, can: { read: true, write: false, share: false, delete: false, change_owner: false }, members: null,
});
const OPTIONS = {
  presets: [{ label: "Every hour", rrule: "FREQ=HOURLY;BYMINUTE=0" }, { label: "Every day at 9:00", rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0" }],
  harnesses: [{ value: "claude", label: "Claude Code" }], default_timezone: "Europe/Warsaw", min_interval_minutes: 5,
  agent_views: [{ id: 3, code: "dev", label: "Dev", workspace_id: 1 }],
};
const FIRES = { fires: [{ at: "2026-11-14T08:00:00Z", local: "2026-11-14T09:00:00" }, { at: "2026-11-15T08:00:00Z", local: "2026-11-15T09:00:00" }] };
const RUNS = [
  { id: 78, status: "RUNNING", created_at: "2026-11-13T10:00:00Z", started_at: "2026-11-13T10:00:01Z", finished_at: null, answer: null },
  { id: 77, status: "SUCCESS", created_at: "2026-11-13T09:00:00Z", started_at: "2026-11-13T09:00:01Z",
    finished_at: "2026-11-13T09:01:13Z", answer: "All **green**: 3 checks passed." },
];
const JOB = {
  id: 77, type: "scheduler", status: "SUCCESS", source: "scheduler", reference_id: "1", agent_view_code: "dev", agent_type: "claude",
  model: "sonnet", input_tokens: 10, output_tokens: 20, created_at: null, started_at: null, finished_at: null, error_class: null,
  error_message: null, result_summary: null, prompt: "the built prompt", output: "the output",
};
const refuse = (status: number, code: string) => new Response(JSON.stringify({ error: code, code }), { status });

afterEach(teardown);

const at = (path: string) => renderAt(path, <Routes>
  <Route path="/schedules" element={<SchedulesPage />} />
  <Route path="/schedules/:id" element={<SchedulePage />} />
  <Route path="/conversations/:id" element={<p>conversation page</p>} />
</Routes>);

describe("the recurrence builder (D-16)", () => {
  const spec = (s: Partial<Spec>): Spec => ({
    freq: "DAILY", interval: 1, time: "09:00", days: [], monthly: "day", monthDay: 1, nth: 1, nthDay: "MO",
    ends: "never", until: "", count: 10, ...s,
  });
  it.each<[Partial<Spec>, string]>([
    [{}, "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"],
    [{ interval: 2, time: "07:05" }, "FREQ=DAILY;INTERVAL=2;BYHOUR=7;BYMINUTE=5"],
    [{ freq: "WEEKLY", days: ["WE", "MO"] }, "FREQ=WEEKLY;BYDAY=MO,WE;BYHOUR=9;BYMINUTE=0"],
    [{ freq: "MONTHLY", monthDay: 15 }, "FREQ=MONTHLY;BYMONTHDAY=15;BYHOUR=9;BYMINUTE=0"],
    [{ freq: "MONTHLY", monthly: "nth", nth: -1, nthDay: "FR" }, "FREQ=MONTHLY;BYDAY=-1FR;BYHOUR=9;BYMINUTE=0"],
    [{ freq: "YEARLY", ends: "after", count: 3 }, "FREQ=YEARLY;BYHOUR=9;BYMINUTE=0;COUNT=3"],
    [{ ends: "on", until: "2027-01-31" }, "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;UNTIL=20270131T235959"],
  ])("%o → %s", (s, rule) => expect(buildRrule(spec(s))).toBe(rule));
});

describe("refusal messages", () => {
  it("knows the fixed target view", () => expect(CODES).toContain("view_fixed"));
  it.each(CODES)("%s has a human message", (code) => {
    const text = message(new ApiError(400, code, undefined, { error: code, code }));
    expect(text).not.toBe(code);
    expect(text.length).toBeGreaterThan(10);
  });
  it("an unknown code shows the server text", () =>
    expect(message(new ApiError(400, "server says no", undefined, { error: "server says no", code: "new_code" }))).toBe("server says no"));
});

describe("the schedules list", () => {
  it("shows the non-empty groups; a Jira row says Custom and is read-only; last_error is human", async () => {
    await stubApi({
      [`${B}/schedules`]: { groups: { mine: [row(1, "Daily digest"), row(2, "Broken", { last_error: "ended", enabled: false })],
        shared: [], agent: [JIRA], user: [] } },
      [`${B}/options`]: OPTIONS,
    });
    at("/schedules");
    expect(await screen.findByRole("link", { name: "Daily digest" })).toHaveAttribute("href", "/schedules/1");
    expect(screen.getByRole("table", { name: "My schedules" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Agent schedules" })).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Shared with me" })).toBeNull();
    const jira = screen.getByRole("link", { name: "PROJ-7 weekly" }).closest("tr")!;
    expect(within(jira).getByText("Custom")).toBeInTheDocument();
    expect(within(jira).getByRole("switch", { name: "Enabled: PROJ-7 weekly" })).toBeDisabled();
    expect(screen.getByText(/has ended/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New schedule" })).toBeInTheDocument();
  });

  it("the Enabled switch sends a PATCH and refreshes", async () => {
    const api = await stubApi({
      [`${B}/schedules`]: { groups: { mine: [row(1, "Daily digest")], shared: [], agent: [], user: [] } },
      [`${B}/options`]: OPTIONS, [`PATCH ${B}/schedules/1`]: null,
    });
    at("/schedules");
    fireEvent.click(await screen.findByRole("switch", { name: "Enabled: Daily digest" }));
    await waitFor(() => expect(api.calls("PATCH", `${B}/schedules/1`)[0]?.body).toEqual({ enabled: false }));
    await waitFor(() => expect(api.calls("GET", `${B}/schedules`).length).toBeGreaterThan(1));
  });
});

// A form test renders the whole page and walks Mantine selects: on a loaded CI host it takes
// longer than the 5 s default (one test waits 5 s itself for the page after a create).
describe("the schedule form", { timeout: 20_000 }, () => {
  it("creates a schedule: a preset sets the RRULE, the preview shows the next fires", async () => {
    const api = await stubApi({ [`${B}/options`]: OPTIONS, [`POST ${B}/preview`]: FIRES, [`POST ${B}/schedules`]: { id: 42 },
      [`${B}/schedules/42`]: row(42, "Digest") });
    at("/schedules/new");
    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "Digest" } });
    fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Summarize" } });
    fireEvent.click(screen.getByLabelText("Repeats", { selector: "input" }));
    fireEvent.click(await screen.findByRole("option", { name: "Every hour" }));
    expect(screen.getByLabelText("RRULE")).toHaveValue("FREQ=HOURLY;BYMINUTE=0");
    await waitFor(() => expect(api.calls("POST", `${B}/preview`)[0]?.body).toEqual({ rrule: "FREQ=HOURLY;BYMINUTE=0", timezone: "Europe/Warsaw" }));
    expect(within(await screen.findByRole("table", { name: "Next runs" })).getAllByRole("row")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    await waitFor(() => expect(api.calls("POST", `${B}/schedules`)[0]?.body).toEqual({
      title: "Digest", prompt: "Summarize", agent_view_id: 3, rrule: "FREQ=HOURLY;BYMINUTE=0", timezone: "Europe/Warsaw",
      harness: null, model: null, enabled: true,
    }));
    // After the create the page navigates to /schedules/42 and loads it; under a loaded
    // test host that can take longer than the 1 s default.
    expect(await screen.findByDisplayValue("Digest", {}, { timeout: 5000 })).toBeInTheDocument();
  });

  it("the preview shows the refusal of a bad rule", async () => {
    await stubApi({ [`${B}/options`]: OPTIONS, [`POST ${B}/preview`]: refuse(400, "too_frequent") });
    at("/schedules/new");
    const rule = await screen.findByLabelText("RRULE");
    fireEvent.change(rule, { target: { value: "FREQ=MINUTELY" } });
    fireEvent.blur(rule);
    expect(await screen.findByText(/too close together/)).toBeInTheDocument();
  });

  it("the Custom dialog writes the RRULE", async () => {
    await stubApi({ [`${B}/options`]: OPTIONS, [`POST ${B}/preview`]: FIRES });
    at("/schedules/new");
    fireEvent.click(await screen.findByLabelText("Repeats", { selector: "input" }));
    fireEvent.click(await screen.findByRole("option", { name: "Custom…" }));
    const dialog = await screen.findByRole("dialog", { name: "Custom recurrence" });
    fireEvent.click(within(dialog).getByLabelText("Repeat", { selector: "input" }));
    fireEvent.click(await screen.findByRole("option", { name: "weeks" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Monday" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Thursday" }));
    fireEvent.change(within(dialog).getByLabelText("Time"), { target: { value: "10:30" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.getByLabelText("RRULE")).toHaveValue("FREQ=WEEKLY;BYDAY=MO,TH;BYHOUR=10;BYMINUTE=30");
  });

  it("an edit sends only the changed fields; Run now waits while the form is dirty (D-21)", async () => {
    const api = await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/1`]: row(1, "Digest"), [`POST ${B}/preview`]: FIRES,
      [`PATCH ${B}/schedules/1`]: null, [`POST ${B}/schedules/1/run`]: { job_id: 77, conversation_id: 9 } });
    at("/schedules/1");
    const title = await screen.findByLabelText("Title");
    const runNow = async () => {
      // The menu stays open across checks; a second click on Actions would close it.
      if (!screen.queryByRole("menuitem", { name: "Run now" })) fireEvent.click(screen.getByRole("button", { name: "Actions" }));
      return screen.findByRole("menuitem", { name: "Run now" });
    };
    expect(await runNow()).not.toBeDisabled();
    fireEvent.change(title, { target: { value: "Digest 2" } });
    expect(await runNow()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.calls("PATCH", `${B}/schedules/1`)[0]?.body).toEqual({ title: "Digest 2" }));
    fireEvent.click(await waitFor(async () => {
      const item = await runNow();
      expect(item).not.toBeDisabled();
      return item;
    }));
    expect(await screen.findByText("conversation page")).toBeInTheDocument();
    expect(api.calls("POST", `${B}/schedules/1/run`)).toHaveLength(1);
  });

  it("a poll refreshes the fields the user did not edit and keeps the edits (UI-8)", async () => {
    let current = row(1, "Digest");
    const api = await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/1`]: () => current, [`POST ${B}/preview`]: FIRES,
      [`PATCH ${B}/schedules/1`]: null });
    at("/schedules/1");
    const title = await screen.findByLabelText("Title");
    const poll = async (next: ReturnType<typeof row>) => {
      current = next;
      await queryClient.invalidateQueries({ queryKey: ["scheduler", "schedule", "1"] });
    };
    await poll(row(1, "Digest", { prompt: "Say bye", enabled: false }));
    await waitFor(() => expect(screen.getByLabelText("Prompt")).toHaveValue("Say bye"));
    expect(screen.getByRole("switch", { name: "Enabled" })).not.toBeChecked();
    fireEvent.change(title, { target: { value: "My title" } });
    await poll(row(1, "Server title", { prompt: "Say ciao", enabled: false }));
    await waitFor(() => expect(screen.getByLabelText("Prompt")).toHaveValue("Say ciao"));
    expect(title).toHaveValue("My title");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.calls("PATCH", `${B}/schedules/1`)[0]?.body).toEqual({ title: "My title" }));
  });

  it("the target view is set at create: open on a new schedule, fixed on an existing one", async () => {
    await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/1`]: row(1, "Digest"), [`POST ${B}/preview`]: FIRES });
    const view = () => screen.getByLabelText("Agent view", { selector: "input" });
    const { unmount } = at("/schedules/new");
    await screen.findByLabelText("Title");
    expect(view()).not.toBeDisabled();
    unmount();
    at("/schedules/1");
    expect(await screen.findByDisplayValue("Digest")).toBeInTheDocument();
    expect(view()).toBeDisabled();
    expect(screen.getByText("Set at create. To move, delete and create again.")).toBeInTheDocument();
  });

  it("a refused Run now shows why", async () => {
    await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/1`]: row(1, "Digest"), [`POST ${B}/preview`]: FIRES,
      [`POST ${B}/schedules/1/run`]: refuse(409, "overlap") });
    at("/schedules/1");
    fireEvent.click(await screen.findByRole("button", { name: "Actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Run now" }));
    expect(await screen.findByText(/previous run has not ended/)).toBeInTheDocument();
  });

  it("a Jira row is read-only: no save and no write action", async () => {
    await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/5`]: JIRA, [`POST ${B}/preview`]: FIRES });
    at("/schedules/5");
    expect(await screen.findByLabelText("Title")).toBeDisabled();
    expect(screen.getByText(/read-only/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Actions" })).toBeNull();
  });

  it("shares with a user and removes a member", async () => {
    const api = await stubApi({ [`${B}/options`]: OPTIONS, [`POST ${B}/preview`]: FIRES,
      [`${B}/schedules/1`]: row(1, "Digest", { members: [{ user_id: 4, username: "bob" }] }),
      [`${B}/schedules/1/member-options`]: [{ id: 4, username: "bob" }, { id: 6, username: "cid" }],
      [`PUT ${B}/schedules/1/members`]: null, [`DELETE ${B}/schedules/1/members/4`]: null });
    at("/schedules/1");
    fireEvent.click(await screen.findByRole("button", { name: "Actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Share" }));
    const dialog = await screen.findByRole("dialog", { name: "Share" });
    fireEvent.click(within(dialog).getByLabelText("Add a user", { selector: "input" }));
    expect(screen.queryByRole("option", { name: "bob" })).toBeNull();
    fireEvent.click(await screen.findByRole("option", { name: "cid" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(api.calls("PUT", `${B}/schedules/1/members`)[0]?.body).toEqual({ username: "cid" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove bob" }));
    await waitFor(() => expect(api.calls("DELETE", `${B}/schedules/1/members/4`)).toHaveLength(1));
  });

  it("an admin changes the owner", async () => {
    const api = await stubApi({ [`${B}/options`]: OPTIONS, [`POST ${B}/preview`]: FIRES, [`${B}/schedules/1`]: row(1, "Digest"),
      [`${B}/schedules/1/member-options`]: [{ id: 6, username: "cid" }], [`POST ${B}/schedules/1/owner`]: null });
    at("/schedules/1");
    fireEvent.click(await screen.findByRole("button", { name: "Actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Change owner" }));
    const dialog = await screen.findByRole("dialog", { name: "Change owner" });
    fireEvent.click(within(dialog).getByLabelText("New owner", { selector: "input" }));
    fireEvent.click(await screen.findByRole("option", { name: "cid" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Change owner" }));
    await waitFor(() => expect(api.calls("POST", `${B}/schedules/1/owner`)[0]?.body).toEqual({ user_id: 6 }));
  });

  it("deletes after a confirm and goes back to the list", async () => {
    const api = await stubApi({ [`${B}/options`]: OPTIONS, [`POST ${B}/preview`]: FIRES, [`${B}/schedules/1`]: row(1, "Digest"),
      [`DELETE ${B}/schedules/1`]: null, [`${B}/schedules`]: { groups: { mine: [], shared: [], agent: [], user: [] } } });
    at("/schedules/1");
    fireEvent.click(await screen.findByRole("button", { name: "Actions" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Delete schedule" })).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.calls("DELETE", `${B}/schedules/1`)).toHaveLength(1));
    expect(await screen.findByRole("button", { name: "New schedule" })).toBeInTheDocument();
  });

  it("a refusal about a field opens its section and shows under that field", async () => {
    await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/1`]: row(1, "Digest"), [`POST ${B}/preview`]: FIRES,
      [`${B}/schedules/1/runs`]: [], [`PATCH ${B}/schedules/1`]: refuse(400, "bad_model") });
    at("/schedules/1");
    const runtime = await screen.findByRole("button", { name: /Agent runtime/ });
    expect(runtime).toHaveAttribute("aria-expanded", "false");
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(runtime).toHaveAttribute("aria-expanded", "true"));
    expect(screen.getByLabelText("Model")).toHaveAccessibleDescription(/model name is not valid/);
  });

  it("Advanced opens by itself for a rule no preset names", async () => {
    await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/5`]: JIRA, [`POST ${B}/preview`]: FIRES, [`${B}/schedules/5/runs`]: [] });
    at("/schedules/5");
    expect(await screen.findByRole("button", { name: /Advanced/ })).toHaveAttribute("aria-expanded", "true");
  });

  it("on an existing schedule the switch applies at once", async () => {
    const api = await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/1`]: row(1, "Digest"), [`POST ${B}/preview`]: FIRES,
      [`${B}/schedules/1/runs`]: [], [`PATCH ${B}/schedules/1`]: null });
    at("/schedules/1");
    fireEvent.click(await screen.findByRole("switch", { name: "Enabled" }));
    await waitFor(() => expect(api.calls("PATCH", `${B}/schedules/1`)[0]?.body).toEqual({ enabled: false }));
  });

  it("last runs: the eye previews the answer and links the conversation; job details need admin.jobs", async () => {
    await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/1`]: row(1, "Digest"), [`POST ${B}/preview`]: FIRES,
      [`${B}/schedules/1/runs`]: RUNS, "/api/admin/jobs/77": JOB });
    at("/schedules/1");
    const runs = await screen.findByRole("table", { name: "Last runs" });
    expect(await within(runs).findByText("All green: 3 checks passed.")).toBeInTheDocument();
    expect(within(runs).getByText("Running…")).toBeInTheDocument();
    fireEvent.click(within(runs).getByRole("button", { name: "Job 77 details" }));
    expect(await screen.findByText("the built prompt")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(within(runs).getByRole("button", { name: "Preview run 77" }));
    const drawer = await screen.findByRole("dialog", { name: /Run 77/ });
    expect(await within(drawer).findByText("green")).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "Open conversation" }));
    expect(await screen.findByText("conversation page")).toBeInTheDocument();
  });

  it("last runs: no job details icon without admin.jobs", async () => {
    await stubApi({ [`${B}/options`]: OPTIONS, [`${B}/schedules/1`]: row(1, "Digest"), [`POST ${B}/preview`]: FIRES,
      [`${B}/schedules/1/runs`]: RUNS }, []);
    at("/schedules/1");
    const runs = await screen.findByRole("table", { name: "Last runs" });
    expect(await within(runs).findByRole("button", { name: "Preview run 77" })).toBeInTheDocument();
    expect(within(runs).queryByRole("button", { name: /details/ })).toBeNull();
  });
});
