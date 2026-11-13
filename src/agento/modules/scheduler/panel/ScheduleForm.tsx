// One schedule (PRD §11, D-34): the form, the preview of its next runs, and its actions — Run now
// (D-21, disabled while the form is dirty), Open conversation, Share, Change owner, Delete.
// `/schedules/new` creates one. The API checks every right; the page hides what `can` refuses.
import { useState } from "react";
import { useNavigate, useParams } from "react-router";
import { apiFetch, LIVE_REFETCH_MS, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  Button, ConfirmDialog, DeleteAction, ErrorState, LoadingState, PageHeader, RowMenu, SectionHeader, SelectField, TextField,
  Timestamp, type RowAction,
} from "@agento/ui";
import { BASE, explain, message, scheduleUrl, useOptions, type Fire, type Options, type Schedule } from "./api";
import { RecurrenceDialog } from "./RecurrenceDialog";

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
  const [failure, setFailure] = useState<string | null>(null);
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
    onError: (e) => setFailure(message(e)),
  });
  const run = useMutation({
    mutationFn: (id: number) => apiFetch<{ job_id: number; conversation_id: number }>(`${scheduleUrl(id)}/run`, { method: "POST" }),
    onSuccess: (r) => { void refresh(); navigate(`/conversations/${r.conversation_id}`); },
    onError: (e) => setFailure(message(e)),
  });
  const remove = useMutation({
    mutationFn: (id: number) => apiFetch(scheduleUrl(id), { method: "DELETE" }),
    // Away first: a refetch of the deleted row would answer 404.
    onSuccess: () => { navigate("/schedules"); void refresh(); },
    onError: (e) => { setDialog(null); setFailure(message(e)); },
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

  return (
    <div className="ag-stack">
      <PageHeader title={row ? row.title : "New schedule"} actions={<RowMenu actions={actions} />}
        description={row ? <>
          Owner {row.owner_username ?? `agent ${row.agent_view_code ?? ""}`} · Next run{" "}
          {row.enabled && row.next_fire_at ? <Timestamp value={row.next_fire_at} /> : "none"} · Last run{" "}
          {row.last_fire_at ? <Timestamp value={row.last_fire_at} /> : "never"}
          {row.last_run_status && ` (${row.last_run_status})`}
        </> : "A prompt that runs an agent on a recurrence."} />
      {row?.last_error && <p className="ag-field__error" role="alert">{explain(row.last_error)}</p>}
      {readOnly && (
        <p className="ag-muted">{row.source !== "scheduler"
          ? `This schedule comes from ${row.source}${row.source_ref ? ` (${row.source_ref})` : ""}. It is read-only here.`
          : "You may see this schedule, but not change it."}</p>
      )}
      <form className="ag-stack" onSubmit={(e) => { e.preventDefault(); save.mutate(form); }}>
        <fieldset className="ag-stack" disabled={readOnly}>
          <TextField label="Title" maxLength={500} value={form.title} hint="Also the title of its conversation."
            onChange={(e) => set({ title: e.target.value })} />
          <SelectField label="Agent view" name="agent_view" value={form.agent_view_id} options={views} disabled={row !== null}
            hint={row ? "Set at create. To move, delete and create again." : undefined}
            onChange={(v) => set({ agent_view_id: v })} />
          <div className="ag-field">
            <label className="ag-field__label" htmlFor="schedule-prompt">Prompt</label>
            <textarea id="schedule-prompt" className="ag-field__input" rows={6} value={form.prompt}
              onChange={(e) => set({ prompt: e.target.value })} />
          </div>
          <SelectField label="Recurrence" name="recurrence" placeholder="Custom" disabled={readOnly}
            value={options.presets.find((p) => p.rrule === form.rrule)?.rrule ?? ""} onChange={pickRecurrence}
            options={[...options.presets.map((p) => ({ value: p.rrule, label: p.label })), { value: CUSTOM, label: "Custom…" }]} />
          <TextField label="RRULE" value={form.rrule} onBlur={() => commit()} onChange={(e) => set({ rrule: e.target.value })}
            hint={`The recurrence as an RRULE (RFC 5545). At least ${options.min_interval_minutes} minutes between runs.`} />
          <div className="ag-row">
            <TextField label="Starts at" type="datetime-local" value={form.starts_at} hint="Local time in the time zone. Empty: now."
              onChange={(e) => { const f = { ...form, starts_at: e.target.value }; setForm(f); commit(f); }} />
            <TextField label="Time zone" value={form.timezone} onBlur={() => commit()} hint="An IANA name, such as Europe/Warsaw."
              onChange={(e) => set({ timezone: e.target.value })} />
          </div>
          <label className="ag-row">
            <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> Enabled
          </label>
          <div className="ag-row">
            <SelectField label="Harness" name="harness" placeholder="Agent view default" value={form.harness} disabled={readOnly}
              options={[{ value: DEFAULT, label: "Agent view default" }, ...options.harnesses]}
              onChange={(v) => set({ harness: v === DEFAULT ? "" : v })} />
            <TextField label="Model" value={form.model} hint="Empty: the agent view's model." maxLength={128}
              onChange={(e) => set({ model: e.target.value })} />
          </div>
        </fieldset>
        <SectionHeader>Next runs</SectionHeader>
        {!checked.rrule.trim() ? <p className="ag-muted">Pick a recurrence to see its next runs.</p>
          : preview.error ? <p className="ag-field__error" role="alert">{message(preview.error)}</p>
            : !preview.data ? <LoadingState />
              : preview.data.fires.length === 0 ? <p className="ag-muted">It does not fire again.</p>
                : <ul className="ag-stack">{preview.data.fires.map((f) => (
                  <li key={f.at}><Timestamp value={f.at} /> ({f.local.replace("T", " ").slice(0, 16)} {checked.timezone})</li>
                ))}</ul>}
        {failure && <p className="ag-field__error" role="alert">{failure}</p>}
        {!readOnly && (
          <div className="ag-row">
            <Button type="submit" variant="primary" disabled={save.isPending || (row !== null && !dirty)}>
              {row ? "Save" : "Create schedule"}
            </Button>
          </div>
        )}
      </form>
      <RecurrenceDialog opened={dialog === "custom"} onCancel={() => setDialog(null)}
        onDone={(rrule) => { setDialog(null); pickRecurrence(rrule); }} />
      {row && dialog === "share" && <ShareDialog row={row} onClose={() => setDialog(null)} />}
      {row && dialog === "owner" && <OwnerDialog row={row} onClose={() => setDialog(null)} />}
      <ConfirmDialog opened={dialog === "delete"} title="Delete schedule" confirmLabel="Delete" danger busy={remove.isPending}
        onCancel={() => setDialog(null)} onConfirm={() => row && remove.mutate(row.id)}>
        <p>The schedule stops and is gone. Its conversation stays.</p>
      </ConfirmDialog>
    </div>
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
    <ConfirmDialog opened title="Share" confirmLabel="Done" onConfirm={onClose} onCancel={onClose}>
      <p className="ag-muted">A member may see, edit and run this schedule, and read its conversation.</p>
      {members.length === 0 ? <p className="ag-muted">Not shared.</p> : (
        <ul className="ag-stack">{members.map((m) => (
          <li key={m.user_id} className="ag-row">{m.username}
            <DeleteAction label={`Remove ${m.username}`} disabled={drop.isPending} onClick={() => drop.mutate(m.user_id)} />
          </li>
        ))}</ul>
      )}
      <div className="ag-row">
        <SelectField label="Add a user" name="member" searchable placeholder="Search" value={pick} onChange={setPick}
          options={(users.data ?? []).filter((u) => !taken.has(u.username)).map((u) => ({ value: u.username, label: u.username }))} />
        <Button disabled={!pick || add.isPending} onClick={() => { add.mutate(pick); setPick(""); }}>Add</Button>
      </div>
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
