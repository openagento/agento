// One schedule (PRD §11, D-34): the form, the preview of its next runs, and its actions — Run now
// (D-21, disabled while the form is dirty), Open conversation, Share, Change owner, Delete.
// `/schedules/new` creates one. The API checks every right; the page hides what `can` refuses.
import { useState } from "react";
import { useNavigate, useParams } from "react-router";
import { apiFetch, LIVE_REFETCH_MS, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  Button, ConfirmDialog, DataTable, DeleteAction, ErrorState, FieldRow, LoadingState, PageColumn, PageHeader, RowMenu, Section, SectionHeader,
  Sections, SelectField, SwitchField, TextAreaField, TextField, Timestamp, type RowAction,
} from "@agento/ui";
import { BASE, codeOf, explain, message, scheduleUrl, useOptions, type Fire, type Options, type Schedule } from "./api";
import { RecurrenceDialog } from "./RecurrenceDialog";
import { LastRuns } from "./Runs";

interface Form {
  title: string; prompt: string; agent_view_id: string; rrule: string; starts_at: string; timezone: string;
  harness: string; model: string; enabled: boolean;
}
type Timing = Pick<Form, "rrule" | "starts_at" | "timezone">;
interface User { id: number; username: string }

const CUSTOM = "__custom__";
const DEFAULT = "__default__";

const toForm = (s: Schedule | null, o: Options): Form => (s ? {
  title: s.title, prompt: s.prompt, agent_view_id: s.agent_view_id ? String(s.agent_view_id) : "", rrule: s.rrule,
  starts_at: s.starts_at?.slice(0, 16) ?? "", timezone: s.timezone, harness: s.harness ?? "", model: s.model ?? "",
  enabled: s.enabled,
} : {
  title: "", prompt: "", agent_view_id: o.agent_views[0] ? String(o.agent_views[0].id) : "", rrule: "", starts_at: "",
  timezone: o.default_timezone, harness: "", model: "", enabled: true,
});

/** The API body. An empty start is left out (the server takes now); an empty pin is null. */
const toBody = (f: Form): Record<string, unknown> => ({
  title: f.title, prompt: f.prompt, ...(f.agent_view_id ? { agent_view_id: Number(f.agent_view_id) } : {}), rrule: f.rrule,
  ...(f.starts_at ? { starts_at: f.starts_at } : {}), timezone: f.timezone, harness: f.harness || null,
  model: f.model.trim() || null, enabled: f.enabled,
});

/** Only the changed fields: a timing field in a PATCH makes the server check the rule again. */
const changes = (f: Form, base: Form) => {
  const was = toBody(base);
  return Object.fromEntries(Object.entries(toBody(f)).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(was[k])));
};

const timing = (f: Form): Timing => ({ rrule: f.rrule, starts_at: f.starts_at, timezone: f.timezone });

/** The field a refusal code is about, and the section that holds each field. */
const FIELD_OF: Record<string, keyof Form> = {
  bad_title: "title", bad_prompt: "prompt", bad_harness: "harness", bad_model: "model", bad_start: "starts_at",
  too_far_back: "starts_at", bad_timezone: "timezone", bad_rule: "rrule", too_frequent: "rrule", no_occurrence: "rrule",
  too_complex: "rrule", ended: "rrule",
};
const SECTION_OF: Record<keyof Form, string> = {
  title: "task", prompt: "task", agent_view_id: "task", starts_at: "when", timezone: "when", enabled: "when",
  harness: "runtime", model: "runtime", rrule: "advanced",
};

/** The browser's IANA zones, with the row's own zone kept when the browser does not list it (UTC). */
const ZONES = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
const zones = (current: string) => [...new Set([current, "UTC", ...ZONES])].filter(Boolean).map((z) => ({ value: z, label: z }));

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
/** "in 25 minutes", "in 3 hours", "tomorrow". */
function fromNow(at: string, now = Date.now()): string {
  const minutes = Math.round((Date.parse(at) - now) / 60_000);
  if (Math.abs(minutes) < 60) return RELATIVE.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  return Math.abs(hours) < 36 ? RELATIVE.format(hours, "hour") : RELATIVE.format(Math.round(hours / 24), "day");
}

export default function SchedulePage() {
  const id = useParams().id ?? "new";
  const isNew = id === "new";
  const options = useOptions();
  const row = useQuery({
    queryKey: ["scheduler", "schedule", id], enabled: !isNew, refetchInterval: LIVE_REFETCH_MS,
    queryFn: ({ signal }) => apiFetch<Schedule>(scheduleUrl(id), { signal }),
  });
  const error = options.error ?? row.error;
  if (error) return <ErrorState message={message(error)} onRetry={() => { void options.refetch(); void row.refetch(); }} />;
  if (!options.data || (!isNew && !row.data)) return <LoadingState />;
  // Keyed per schedule; within one, a poll refreshes only the fields the user did not edit (UI-8).
  return <Editor key={id} row={isNew ? null : row.data!} options={options.data} />;
}

function Editor({ row, options }: { row: Schedule | null; options: Options }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["scheduler"] });
  const [base, setBase] = useState(() => toForm(row, options));
  const [form, setForm] = useState(base);
  const set = (patch: Partial<Form>) => setForm({ ...form, ...patch });
  // The preview follows the timing fields on a commit (blur, pick), not on every key: each check
  // runs the engine in a child process on the server.
  const [checked, setChecked] = useState(() => timing(base));
  const commit = (f = form) => setChecked(timing(f));
  // A new row from the poll: a clean field (form equals base) takes the new value, an edit stays.
  const [seen, setSeen] = useState(row);
  if (row && row !== seen) {
    setSeen(row);
    const next = toForm(row, options);
    const keys = Object.keys(next) as (keyof Form)[];
    setForm(Object.fromEntries(keys.map((k) => [k, form[k] === base[k] ? next[k] : form[k]])) as unknown as Form);
    setBase(next);
    if ((["rrule", "starts_at", "timezone"] as const).every((k) => form[k] === base[k])) setChecked(timing(next));
  }
  const preset = options.presets.find((p) => p.rrule === form.rrule);
  // Advanced opens by itself for a rule no preset names: the RRULE is then the only place it shows.
  const [open, setOpen] = useState(() => ["task", "when", ...(base.rrule && !preset ? ["advanced"] : [])]);
  const [failure, setFailure] = useState<{ field: keyof Form | null; text: string } | null>(null);
  const fail = (e: unknown) => {
    const field = FIELD_OF[codeOf(e) ?? ""] ?? null;
    setFailure({ field, text: message(e) });
    // A refusal names a field: open its section, or the error would hide in a closed one.
    if (field && !open.includes(SECTION_OF[field])) setOpen([...open, SECTION_OF[field]]);
  };
  const errorOf = (field: keyof Form) => (failure?.field === field ? failure.text : null);
  const [dialog, setDialog] = useState<null | "custom" | "share" | "owner" | "delete">(null);
  const readOnly = row !== null && !row.can.write;
  const diff = changes(form, base);
  const dirty = Object.keys(diff).length > 0;

  const preview = useQuery({
    queryKey: ["scheduler-preview", checked], enabled: checked.rrule.trim() !== "", retry: false, staleTime: Infinity,
    queryFn: ({ signal }) => apiFetch<{ fires: Fire[] }>(`${BASE}/preview`, { method: "POST", signal, json: {
      rrule: checked.rrule, timezone: checked.timezone, ...(checked.starts_at ? { starts_at: checked.starts_at } : {}),
    } }),
  });
  const save = useMutation({
    mutationFn: (f: Form) => (row
      ? apiFetch(scheduleUrl(row.id), { method: "PATCH", json: changes(f, base) })
      : apiFetch<{ id: number }>(`${BASE}/schedules`, { method: "POST", json: toBody(f) })),
    onSuccess: (r, f) => {
      setFailure(null);
      void refresh();
      if (row) setBase(f);
      else navigate(`/schedules/${(r as { id: number }).id}`);
    },
    onError: fail,
  });
  // On an existing schedule the switch applies at once, as a switch does; on a new one it is a field.
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => apiFetch(scheduleUrl(row!.id), { method: "PATCH", json: { enabled } }),
    onSuccess: () => setFailure(null),
    onError: fail,
    onSettled: () => refresh(),
  });
  const run = useMutation({
    mutationFn: (id: number) => apiFetch<{ job_id: number; conversation_id: number }>(`${scheduleUrl(id)}/run`, { method: "POST" }),
    onSuccess: (r) => { void refresh(); navigate(`/conversations/${r.conversation_id}`); },
    onError: fail,
  });
  const remove = useMutation({
    mutationFn: (id: number) => apiFetch(scheduleUrl(id), { method: "DELETE" }),
    // Away first: a refetch of the deleted row would answer 404.
    onSuccess: () => { navigate("/schedules"); void refresh(); },
    onError: (e) => { setDialog(null); fail(e); },
  });

  const actions: RowAction[] = row ? [
    ...(row.can.write ? [{ label: "Run now", disabled: dirty || run.isPending, onClick: () => run.mutate(row.id) }] : []),
    ...(row.conversation_id ? [{ label: "Open conversation", onClick: () => navigate(`/conversations/${row.conversation_id}`) }] : []),
    ...(row.can.share ? [{ label: "Share", onClick: () => setDialog("share") }] : []),
    ...(row.can.change_owner ? [{ label: "Change owner", onClick: () => setDialog("owner") }] : []),
    ...(row.can.delete ? [{ label: "Delete", danger: true, onClick: () => setDialog("delete") }] : []),
  ] : [];
  const views = options.agent_views.map((v) => ({ value: String(v.id), label: v.label || v.code }));
  if (row?.agent_view_id && !views.some((v) => v.value === String(row.agent_view_id))) {
    views.push({ value: String(row.agent_view_id), label: row.agent_view_code ?? `view ${row.agent_view_id}` });
  }
  const pickRecurrence = (v: string) => {
    if (v === CUSTOM) return setDialog("custom");
    const f = { ...form, rrule: v };
    setForm(f);
    commit(f);
  };
  const enabled = row ? (toggle.isPending ? toggle.variables : row.enabled) : form.enabled;
  const harnessLabel = options.harnesses.find((h) => h.value === form.harness)?.label ?? form.harness;

  return (
    <PageColumn>
      <PageHeader title={row ? row.title : "New schedule"}
        actions={<>
          <SwitchField label="Enabled" checked={enabled} disabled={readOnly || toggle.isPending}
            onChange={(on) => (row ? toggle.mutate(on) : set({ enabled: on }))} />
          <RowMenu actions={actions} />
        </>}
        description={row ? <>
          {row.owner_username ? `Owned by ${row.owner_username}` : `Owned by agent ${row.agent_view_code ?? ""}`}
          {" · "}{!row.enabled ? "Paused" : row.next_fire_at ? <>Next run <Timestamp value={row.next_fire_at} /></> : "No next run"}
        </> : "A prompt that runs an agent on a recurrence."} />
      {row?.last_error && <p className="ag-field__error" role="alert">{explain(row.last_error)}</p>}
      {readOnly && (
        <p className="ag-muted">{row.source !== "scheduler"
          ? `This schedule comes from ${row.source}${row.source_ref ? ` (${row.source_ref})` : ""}. It is read-only here.`
          : "You may see this schedule, but not change it."}</p>
      )}
      <form className="ag-stack" onSubmit={(e) => { e.preventDefault(); save.mutate(form); }}>
        <fieldset className="ag-stack" disabled={readOnly}>
          <Sections value={open} onChange={setOpen}>
            <Section value="task" title="Task" description="What the agent does"
              summary={views.find((v) => v.value === form.agent_view_id)?.label}>
              <TextField label="Title" maxLength={500} value={form.title} hint="Also the title of its conversation."
                error={errorOf("title")} onChange={(e) => set({ title: e.target.value })} />
              <SelectField label="Agent view" name="agent_view" value={form.agent_view_id} options={views} disabled={row !== null}
                hint={row ? "Set at create. To move, delete and create again." : "The agent that runs the prompt."}
                onChange={(v) => set({ agent_view_id: v })} />
              <TextAreaField label="Prompt" value={form.prompt} error={errorOf("prompt")} minRows={5}
                hint="Each run starts from this prompt, in the same conversation." onChange={(e) => set({ prompt: e.target.value })} />
            </Section>
            <Section value="when" title="Schedule" description="When it runs"
              summary={`${preset?.label ?? (form.rrule ? "Custom" : "Not set")} · ${form.timezone}`}>
              <SelectField label="Repeats" name="recurrence" placeholder="Pick a recurrence" disabled={readOnly} value={form.rrule}
                onChange={pickRecurrence} hint={preset || !form.rrule ? undefined : "A custom rule. See it under Advanced."}
                options={[...options.presets.map((p) => ({ value: p.rrule, label: p.label })),
                  ...(form.rrule && !preset ? [{ value: form.rrule, label: "Custom rule" }] : []), { value: CUSTOM, label: "Custom…" }]} />
              <FieldRow>
                <TextField label="Starts" type="datetime-local" value={form.starts_at} hint="In the time zone. Empty: now."
                  error={errorOf("starts_at")}
                  onChange={(e) => { const f = { ...form, starts_at: e.target.value }; setForm(f); commit(f); }} />
                <SelectField label="Time zone" name="timezone" searchable value={form.timezone} options={zones(form.timezone)}
                  disabled={readOnly} error={errorOf("timezone")}
                  onChange={(v) => { const f = { ...form, timezone: v }; setForm(f); commit(f); }} />
              </FieldRow>
            </Section>
            <Section value="runtime" title="Agent runtime" description="The harness and model that run it"
              summary={[form.harness ? harnessLabel : "Default harness", form.model.trim() || "default model"].join(" · ")}>
              <FieldRow>
                <SelectField label="Harness" name="harness" value={form.harness || DEFAULT} disabled={readOnly}
                  hint="Default: the agent view's harness." error={errorOf("harness")}
                  options={[{ value: DEFAULT, label: "Agent view default" }, ...options.harnesses]}
                  onChange={(v) => set({ harness: v === DEFAULT ? "" : v })} />
                <TextField label="Model" value={form.model} placeholder="Agent view default" maxLength={128}
                  hint="Empty: the agent view's model." error={errorOf("model")} onChange={(e) => set({ model: e.target.value })} />
              </FieldRow>
            </Section>
            <Section value="advanced" title="Advanced" description="The recurrence as an RRULE" summary={form.rrule || undefined}>
              <TextField label="RRULE" value={form.rrule} onBlur={() => commit()} error={errorOf("rrule")}
                onChange={(e) => set({ rrule: e.target.value })} placeholder="FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0"
                hint={`RFC 5545. Any rule the presets do not offer. At least ${options.min_interval_minutes} minutes between runs.`} />
            </Section>
          </Sections>
        </fieldset>
        {failure && !failure.field && <p className="ag-field__error" role="alert">{failure.text}</p>}
        {!readOnly && (
          <div className="ag-row">
            <Button type="submit" variant="primary" disabled={save.isPending || (row !== null && !dirty)}>
              {row ? "Save changes" : "Create schedule"}
            </Button>
            {row && dirty && (
              <Button variant="subtle" onClick={() => { setForm(base); commit(base); setFailure(null); }}>Discard changes</Button>
            )}
          </div>
        )}
      </form>
      {row && <><SectionHeader>Last runs</SectionHeader><LastRuns row={row} /></>}
      <SectionHeader>Next runs</SectionHeader>
      {!enabled && row ? <p className="ag-muted">Paused. Turn it on to run again; these are the times it would run.</p> : null}
      {!checked.rrule.trim() ? <p className="ag-muted">Pick when it repeats to see its next runs.</p>
        : preview.error ? <p className="ag-field__error" role="alert">{message(preview.error)}</p>
          : !preview.data ? <LoadingState />
            : preview.data.fires.length === 0 ? <p className="ag-muted">It does not run again.</p>
              : <DataTable caption="Next runs" rows={preview.data.fires} rowKey={(f) => f.at} columns={[
                { id: "at", header: "When", cell: (f) => <Timestamp value={f.at} /> },
                { id: "in", header: "In", nowrap: true, cell: (f) => fromNow(f.at) },
                { id: "local", header: `Local time (${checked.timezone})`, cell: (f) => f.local.replace("T", " ").slice(0, 16) },
              ]} />}
      <RecurrenceDialog opened={dialog === "custom"} onCancel={() => setDialog(null)}
        onDone={(rrule) => { setDialog(null); pickRecurrence(rrule); }} />
      {row && dialog === "share" && <ShareDialog row={row} onClose={() => setDialog(null)} />}
      {row && dialog === "owner" && <OwnerDialog row={row} onClose={() => setDialog(null)} />}
      <ConfirmDialog opened={dialog === "delete"} title="Delete schedule" confirmLabel="Delete" danger busy={remove.isPending}
        onCancel={() => setDialog(null)} onConfirm={() => row && remove.mutate(row.id)}>
        <p>The schedule stops and is gone. Its conversation stays.</p>
      </ConfirmDialog>
    </PageColumn>
  );
}

/** The users a sharer may pick: active, not the owner. Also the owner change's list (admin). */
const useUsers = (id: number) => useQuery({
  queryKey: ["scheduler", "member-options", id],
  queryFn: ({ signal }) => apiFetch<User[]>(`${scheduleUrl(id)}/member-options`, { signal }),
});

function ShareDialog({ row, onClose }: { row: Schedule; onClose: () => void }) {
  const qc = useQueryClient();
  const users = useUsers(row.id);
  const [pick, setPick] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const done = {
    onSuccess: () => setFailure(null), onError: (e: unknown) => setFailure(message(e)),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["scheduler"] }),
  };
  const add = useMutation({ mutationFn: (username: string) => apiFetch(`${scheduleUrl(row.id)}/members`, { method: "PUT", json: { username } }), ...done });
  const drop = useMutation({ mutationFn: (userId: number) => apiFetch(`${scheduleUrl(row.id)}/members/${userId}`, { method: "DELETE" }), ...done });
  const members = row.members ?? [];
  const taken = new Set(members.map((m) => m.username));
  return (
    <ConfirmDialog opened title="Share" confirmLabel="Done" cancelLabel={null} onConfirm={onClose} onCancel={onClose}>
      <p className="ag-muted">A member may see, edit and run this schedule, and read its conversation.</p>
      <DataTable caption="Members" rows={members} rowKey={(m) => String(m.user_id)} emptyTitle="Not shared yet"
        emptyText="Add a user below." columns={[
          { id: "user", header: "Member", cell: (m) => m.username },
          { id: "remove", header: "Actions", cell: (m) => (
            <DeleteAction label={`Remove ${m.username}`} disabled={drop.isPending} onClick={() => drop.mutate(m.user_id)} />
          ) },
        ]} />
      <FieldRow align="end">
        <SelectField label="Add a user" name="member" searchable placeholder="Search users" value={pick} onChange={setPick}
          options={(users.data ?? []).filter((u) => !taken.has(u.username)).map((u) => ({ value: u.username, label: u.username }))} />
        <Button variant="primary" disabled={!pick || add.isPending} onClick={() => { add.mutate(pick); setPick(""); }}>Add</Button>
      </FieldRow>
      {failure && <p className="ag-field__error" role="alert">{failure}</p>}
    </ConfirmDialog>
  );
}

function OwnerDialog({ row, onClose }: { row: Schedule; onClose: () => void }) {
  const qc = useQueryClient();
  const users = useUsers(row.id);
  const [pick, setPick] = useState("");
  const change = useMutation({
    mutationFn: (userId: number) => apiFetch(`${scheduleUrl(row.id)}/owner`, { method: "POST", json: { user_id: userId } }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["scheduler"] }); onClose(); },
  });
  return (
    <ConfirmDialog opened title="Change owner" confirmLabel="Change owner" busy={!pick || change.isPending}
      onCancel={onClose} onConfirm={() => change.mutate(Number(pick))}>
      <p>The schedule and its conversation move to the new owner. The shares stay.</p>
      <SelectField label="New owner" name="owner" searchable placeholder="Search" value={pick} onChange={setPick}
        options={(users.data ?? []).map((u) => ({ value: String(u.id), label: u.username }))} />
      {change.error && <p className="ag-field__error" role="alert">{message(change.error)}</p>}
    </ConfirmDialog>
  );
}
