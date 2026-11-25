// One schedule (PRD §11, D-34): the form, the preview of its next runs, and its actions — Run now
// (D-21, disabled while the form is dirty), Open conversation, Share, Change owner, Delete.
// `/schedules/new` creates one. The API checks every right; the page hides what `can` refuses.
import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router";
import {
  apiFetch, errorCode, LIVE_REFETCH_MS, useErrorText, useMutation, useQuery, useQueryClient, useUploads, type StagedFile,
} from "@agento/api";
import {
  Button, ConfirmDialog, DataTable, DeleteAction, ErrorState, FieldRow, FileDrop, FilesField, LoadingState, PageColumn, PageHeader, RowMenu, Section, SectionHeader,
  relativeTimeFormat, Sections, SelectField, SwitchField, TextAreaField, TextField, Timestamp, timeZones, useDocumentTitle, useLocale, useT, WallTime, withElements, type RowAction,
} from "@agento/ui";
import { BASE, scheduleUrl, useExplain, useOptions, type Fire, type Options, type Schedule } from "./api";
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

/** The saved files of a row, as the staged list the form starts from. */
const savedFiles = (s: Schedule | null): StagedFile[] => (s?.attachments ?? []).map((a) => (
  { id: a.upload_id, uploadId: a.upload_id, name: a.name, bytes: a.bytes ?? undefined, status: "ready" }));
const keyOf = (ids: string[]) => ids.join();

const timing = (f: Form): Timing => ({ rrule: f.rrule, starts_at: f.starts_at, timezone: f.timezone });

/** The field a refusal code is about, and the section that holds each field. */
const FIELD_OF: Record<string, keyof Form> = {
  bad_title: "title", bad_prompt: "prompt", bad_harness: "harness", bad_model: "model", bad_start: "starts_at",
  too_far_back: "starts_at", bad_timezone: "timezone", bad_rule: "rrule", too_frequent: "rrule", no_occurrence: "rrule",
  too_complex: "rrule", ended: "rrule",
};
const FIELDS_IN: Record<string, (keyof Form)[]> = {
  task: ["title", "prompt", "agent_view_id"], when: ["starts_at", "timezone", "enabled"],
  runtime: ["harness", "model"], advanced: ["rrule"],
};
const sectionOf = (field: keyof Form) => Object.keys(FIELDS_IN).find((section) => FIELDS_IN[section].includes(field))!;
const RRULE_FIELD_NAME = "RRULE";
const RRULE_EXAMPLE = "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0";

/** The browser's IANA zones, with the row's own zone kept when the browser does not list it (UTC). */
const ZONES = timeZones();
const zones = (current: string) => [...new Set([current, "UTC", ...ZONES])].filter(Boolean).map((z) => ({ value: z, label: z }));

/** "in 25 minutes", "in 3 hours", "tomorrow". */
function fromNow(relative: Intl.RelativeTimeFormat, at: string, now = Date.now()): string {
  const minutes = Math.round((Date.parse(at) - now) / 60_000);
  if (Math.abs(minutes) < 60) return relative.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  return Math.abs(hours) < 36 ? relative.format(hours, "hour") : relative.format(Math.round(hours / 24), "day");
}

export default function SchedulePage() {
  const id = useParams().id ?? "new";
  const isNew = id === "new";
  const options = useOptions();
  const row = useQuery({
    queryKey: ["scheduler", "schedule", id], enabled: !isNew, refetchInterval: LIVE_REFETCH_MS,
    queryFn: ({ signal }) => apiFetch<Schedule>(scheduleUrl(id), { signal }),
  });
  const errorText = useErrorText("scheduler");
  const t = useT("scheduler");
  const error = options.error ?? row.error;
  const ready = !error && options.data && (isNew || row.data);
  useDocumentTitle(ready ? undefined : t("list.title"));
  if (error) return <ErrorState message={errorText(error, "error.generic")} onRetry={() => { void options.refetch(); void row.refetch(); }} />;
  if (!ready) return <LoadingState />;
  // Keyed per schedule; within one, a poll refreshes only the fields the user did not edit (UI-8).
  return <Editor key={id} row={isNew ? null : row.data!} options={options.data} />;
}

function Editor({ row, options }: { row: Schedule | null; options: Options }) {
  const t = useT("scheduler");
  const errorText = useErrorText("scheduler");
  const explain = useExplain();
  const locale = useLocale();
  const relative = useMemo(() => relativeTimeFormat(locale, { numeric: "auto" }), [locale]);
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
  // The files: staged to the schedule, or to the user's draft bucket while it has no id. A save
  // names the full wanted set, so the set is dirty when its ids differ from the saved ones.
  const uploads = useUploads(row ? `${scheduleUrl(row.id)}/uploads` : `${BASE}/uploads`, savedFiles(row), "scheduler");
  const [fileBase, setFileBase] = useState(() => keyOf(savedFiles(row).map((f) => f.id)));
  const filesDirty = keyOf(uploads.ids) !== fileBase;
  // A new row from the poll: a clean field (form equals base) takes the new value, an edit stays.
  const [seen, setSeen] = useState(row);
  if (row && row !== seen) {
    setSeen(row);
    if (!filesDirty) {
      const saved = savedFiles(row);
      const key = keyOf(saved.map((f) => f.id));
      if (key !== fileBase) { uploads.reset(saved); setFileBase(key); }
    }
    const next = toForm(row, options);
    const keys = Object.keys(next) as (keyof Form)[];
    setForm(Object.fromEntries(keys.map((k) => [k, form[k] === base[k] ? next[k] : form[k]])) as unknown as Form);
    setBase(next);
    if ((["rrule", "starts_at", "timezone"] as const).every((k) => form[k] === base[k])) setChecked(timing(next));
  }
  const preset = options.presets.find((p) => p.rrule === form.rrule);
  // Advanced opens by itself for a rule no preset names: the RRULE is then the only place it shows.
  const [open, setOpen] = useState(() => ["task", "when", ...(base.rrule && !preset ? ["advanced"] : [])]);
  const [failure, setFailure] = useState<{ field: keyof Form | null; error: unknown } | null>(null);
  const fail = (e: unknown) => {
    const field = FIELD_OF[errorCode(e) ?? ""] ?? null;
    setFailure({ field, error: e });
    // A refusal names a field: open its section, or the error would hide in a closed one.
    if (field && !open.includes(sectionOf(field))) setOpen([...open, sectionOf(field)]);
  };
  const errorOf = (field: keyof Form) => (failure?.field === field ? errorText(failure.error, "error.generic") : null);
  const [dialog, setDialog] = useState<null | "custom" | "share" | "owner" | "delete">(null);
  const readOnly = row !== null && !row.can.write;
  const diff = changes(form, base);
  const dirty = Object.keys(diff).length > 0 || filesDirty;
  const [tooManyFiles, setTooManyFiles] = useState(false);
  const room = options.max_uploads - uploads.files.length;
  const attach = (picked: File[]) => {
    // A drop lands in Task: open it, or the new chips would hide in a closed section.
    if (!open.includes("task")) setOpen([...open, "task"]);
    // Said at the field, where the eye is, not at the submit row a screen below.
    setTooManyFiles(picked.length > room);
    uploads.add(picked.slice(0, Math.max(0, room)));
  };

  const preview = useQuery({
    queryKey: ["scheduler-preview", checked], enabled: checked.rrule.trim() !== "", retry: false, staleTime: Infinity,
    queryFn: ({ signal }) => apiFetch<{ fires: Fire[] }>(`${BASE}/preview`, { method: "POST", signal, json: {
      rrule: checked.rrule, timezone: checked.timezone, ...(checked.starts_at ? { starts_at: checked.starts_at } : {}),
    } }),
  });
  const save = useMutation({
    mutationFn: ({ form: f, ids }: { form: Form; ids: string[] }) => (row
      ? apiFetch(scheduleUrl(row.id), { method: "PATCH", json: { ...changes(f, base), ...(keyOf(ids) !== fileBase ? { upload_ids: ids } : {}) } })
      : apiFetch<{ id: number }>(`${BASE}/schedules`, { method: "POST", json: { ...toBody(f), ...(ids.length ? { upload_ids: ids } : {}) } })),
    onSuccess: (r, { form: f, ids }) => {
      setFailure(null);
      void refresh();
      // A file that failed to upload is not part of what was saved: it must not stay beside the saved ones.
      if (row) { setBase(f); setFileBase(keyOf(ids)); uploads.reset(uploads.files.filter((x) => x.status !== "error")); }
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
    ...(row.can.write ? [{ id: "run_now", label: t("form.action.run_now"), disabled: dirty || run.isPending, onClick: () => run.mutate(row.id) }] : []),
    ...(row.conversation_id ? [{ id: "open_conversation", label: t("action.open_conversation"), onClick: () => navigate(`/conversations/${row.conversation_id}`) }] : []),
    ...(row.can.share ? [{ id: "share", label: t("form.action.share"), onClick: () => setDialog("share") }] : []),
    ...(row.can.change_owner ? [{ id: "change_owner", label: t("form.action.change_owner"), onClick: () => setDialog("owner") }] : []),
    ...(row.can.delete ? [{ id: "delete", label: t("form.action.delete"), danger: true, onClick: () => setDialog("delete") }] : []),
  ] : [];
  const views = options.agent_views.map((v) => ({ value: String(v.id), label: v.label || v.code }));
  if (row?.agent_view_id && !views.some((v) => v.value === String(row.agent_view_id))) {
    views.push({ value: String(row.agent_view_id), label: row.agent_view_code ?? t("form.view_fallback", { id: String(row.agent_view_id) }) });
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
      <PageHeader title={row ? row.title : t("form.new")}
        actions={<>
          <SwitchField label={t("form.enabled")} checked={enabled} disabled={readOnly || toggle.isPending}
            onChange={(on) => (row ? toggle.mutate(on) : set({ enabled: on }))} />
          <RowMenu actions={actions} />
        </>}
        description={row ? <>
          {row.owner_username ? t("form.owned_by", { owner: row.owner_username })
            : t("form.owned_by_agent", { agent: row.agent_view_code ?? "" })}
          {" · "}{!row.enabled ? t("form.paused")
            : row.next_fire_at ? withElements(t("form.next_run"), { time: <Timestamp value={row.next_fire_at} /> }) : t("form.no_next_run")}
        </> : t("form.new_description")} />
      {row?.last_error && <p className="ag-field__error" role="alert">{explain(row.last_error)}</p>}
      {readOnly && (
        <p className="ag-muted">{row.source !== "scheduler"
          ? (row.source_ref ? t("form.from_source_ref", { source: row.source, ref: row.source_ref })
            : t("form.from_source", { source: row.source }))
          : t("form.read_only")}</p>
      )}
      <FileDrop onFiles={readOnly ? undefined : attach} hint={t("form.drop_hint")}>
      <form className="ag-stack" onSubmit={(e) => { e.preventDefault(); save.mutate({ form, ids: uploads.ids }); }}>
        <fieldset className="ag-stack" disabled={readOnly}>
          <Sections value={open} onChange={setOpen}>
            <Section value="task" title={t("form.task.title")} description={t("form.task.description")}
              summary={views.find((v) => v.value === form.agent_view_id)?.label}>
              <TextField label={t("form.title.label")} maxLength={500} value={form.title} hint={t("form.title.hint")}
                error={errorOf("title")} onChange={(e) => set({ title: e.target.value })} />
              <SelectField label={t("form.view.label")} name="agent_view" value={form.agent_view_id} options={views} disabled={row !== null}
                hint={row ? t("form.view.hint_fixed") : t("form.view.hint_new")}
                onChange={(v) => set({ agent_view_id: v })} />
              <TextAreaField label={t("form.prompt.label")} value={form.prompt} error={errorOf("prompt")} minRows={5}
                hint={t("form.prompt.hint")} onChange={(e) => set({ prompt: e.target.value })} />
              <FilesField label={t("form.files.label")} files={uploads.files} onFiles={readOnly ? undefined : attach}
                error={tooManyFiles ? t("form.files.too_many", { count: options.max_uploads }) : null}
                onRemove={(id) => { uploads.remove(id); setTooManyFiles(false); }}
                empty={readOnly ? t("form.files.none") : t("form.files.empty")} hint={t("form.files.hint")} />
            </Section>
            <Section value="when" title={t("form.when.title")} description={t("form.when.description")}
              summary={`${preset ? preset.label : form.rrule ? t("recurrence.custom") : t("form.when.not_set")} · ${form.timezone}`}>
              <SelectField label={t("form.repeats.label")} name="recurrence" placeholder={t("form.repeats.placeholder")} disabled={readOnly} value={form.rrule}
                onChange={pickRecurrence} hint={preset || !form.rrule ? undefined : t("form.repeats.custom_hint")}
                options={[...options.presets.map((p) => ({ value: p.rrule, label: p.label })),
                  ...(form.rrule && !preset ? [{ value: form.rrule, label: t("form.repeats.custom_rule") }] : []),
                  { value: CUSTOM, label: t("form.repeats.custom_pick") }]} />
              <FieldRow>
                <TextField label={t("form.starts.label")} type="datetime-local" value={form.starts_at} hint={t("form.starts.hint")}
                  error={errorOf("starts_at")}
                  onChange={(e) => { const f = { ...form, starts_at: e.target.value }; setForm(f); commit(f); }} />
                <SelectField label={t("form.timezone.label")} name="timezone" searchable value={form.timezone} options={zones(form.timezone)}
                  disabled={readOnly} error={errorOf("timezone")}
                  onChange={(v) => { const f = { ...form, timezone: v }; setForm(f); commit(f); }} />
              </FieldRow>
            </Section>
            <Section value="runtime" title={t("form.runtime.title")} description={t("form.runtime.description")}
              summary={[form.harness ? harnessLabel : t("form.runtime.default_harness"), form.model.trim() || t("form.runtime.default_model")].join(" · ")}>
              <FieldRow>
                <SelectField label={t("form.harness.label")} name="harness" value={form.harness || DEFAULT} disabled={readOnly}
                  hint={t("form.harness.hint")} error={errorOf("harness")}
                  options={[{ value: DEFAULT, label: t("form.harness.default") }, ...options.harnesses]}
                  onChange={(v) => set({ harness: v === DEFAULT ? "" : v })} />
                <TextField label={t("form.model.label")} value={form.model} placeholder={t("form.model.placeholder")} maxLength={128}
                  hint={t("form.model.hint")} error={errorOf("model")} onChange={(e) => set({ model: e.target.value })} />
              </FieldRow>
            </Section>
            <Section value="advanced" title={t("form.advanced.title")} description={t("form.advanced.description")} summary={form.rrule || undefined}>
              <TextField label={RRULE_FIELD_NAME} value={form.rrule} onBlur={() => commit()} error={errorOf("rrule")}
                onChange={(e) => set({ rrule: e.target.value })} placeholder={RRULE_EXAMPLE}
                hint={t("form.rrule.hint", { count: options.min_interval_minutes })} />
            </Section>
          </Sections>
        </fieldset>
        {failure && !failure.field && <p className="ag-field__error" role="alert">{errorText(failure.error, "error.generic")}</p>}
        {!readOnly && (
          <div className="ag-row">
            <Button type="submit" variant="primary" disabled={save.isPending || uploads.busy || (row !== null && !dirty)}>
              {row ? t("form.save") : t("form.create")}
            </Button>
            {row && dirty && (
              <Button variant="subtle" onClick={() => { setForm(base); commit(base); uploads.reset(savedFiles(row)); setFileBase(keyOf(savedFiles(row).map((f) => f.id))); setTooManyFiles(false); setFailure(null); }}>{t("form.discard")}</Button>
            )}
          </div>
        )}
      </form>
      </FileDrop>
      {row && <><SectionHeader>{t("form.last_runs")}</SectionHeader><LastRuns row={row} /></>}
      <SectionHeader>{t("form.next_runs")}</SectionHeader>
      {!enabled && row ? <p className="ag-muted">{t("form.next.paused")}</p> : null}
      {!checked.rrule.trim() ? <p className="ag-muted">{t("form.next.pick")}</p>
        : preview.error ? <p className="ag-field__error" role="alert">{errorText(preview.error, "error.generic")}</p>
          : !preview.data ? <LoadingState />
            : preview.data.fires.length === 0 ? <p className="ag-muted">{t("form.next.none")}</p>
              : <DataTable caption={t("form.next_runs")} rows={preview.data.fires} rowKey={(f) => f.at} columns={[
                { id: "at", header: t("form.next.column.when"), cell: (f) => <Timestamp value={f.at} /> },
                { id: "in", header: t("form.next.column.in"), nowrap: true, cell: (f) => fromNow(relative, f.at) },
                { id: "local", header: t("form.next.column.local", { timezone: checked.timezone }), cell: (f) => <WallTime local={f.local} at={f.at} /> },
              ]} />}
      <RecurrenceDialog opened={dialog === "custom"} onCancel={() => setDialog(null)}
        onDone={(rrule) => { setDialog(null); pickRecurrence(rrule); }} />
      {row && dialog === "share" && <ShareDialog row={row} onClose={() => setDialog(null)} />}
      {row && dialog === "owner" && <OwnerDialog row={row} onClose={() => setDialog(null)} />}
      <ConfirmDialog opened={dialog === "delete"} title={t("form.delete.title")} confirmLabel={t("form.delete.confirm")} danger busy={remove.isPending}
        onCancel={() => setDialog(null)} onConfirm={() => row && remove.mutate(row.id)}>
        <p>{t("form.delete.text")}</p>
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
  const t = useT("scheduler");
  const errorText = useErrorText("scheduler");
  const [pick, setPick] = useState("");
  const [failure, setFailure] = useState<unknown>(null);
  const done = {
    onSuccess: () => setFailure(null), onError: (e: unknown) => setFailure(e),
    onSettled: () => void qc.invalidateQueries({ queryKey: ["scheduler"] }),
  };
  const add = useMutation({ mutationFn: (username: string) => apiFetch(`${scheduleUrl(row.id)}/members`, { method: "PUT", json: { username } }), ...done });
  const drop = useMutation({ mutationFn: (userId: number) => apiFetch(`${scheduleUrl(row.id)}/members/${userId}`, { method: "DELETE" }), ...done });
  const members = row.members ?? [];
  const taken = new Set(members.map((m) => m.username));
  return (
    <ConfirmDialog opened title={t("share.title")} confirmLabel={t("share.done")} cancelLabel={null} onConfirm={onClose} onCancel={onClose}>
      <p className="ag-muted">{t("share.text")}</p>
      <DataTable caption={t("share.members")} rows={members} rowKey={(m) => String(m.user_id)} emptyTitle={t("share.empty.title")}
        emptyText={t("share.empty.text")} columns={[
          { id: "user", header: t("share.column.member"), cell: (m) => m.username },
          { id: "remove", header: t("share.column.actions"), cell: (m) => (
            <DeleteAction label={t("share.remove", { username: m.username })} disabled={drop.isPending} onClick={() => drop.mutate(m.user_id)} />
          ) },
        ]} />
      <FieldRow align="end">
        <SelectField label={t("share.add.label")} name="member" searchable placeholder={t("share.add.placeholder")} value={pick} onChange={setPick}
          options={(users.data ?? []).filter((u) => !taken.has(u.username)).map((u) => ({ value: u.username, label: u.username }))} />
        <Button variant="primary" disabled={!pick || add.isPending} onClick={() => { add.mutate(pick); setPick(""); }}>{t("share.add.submit")}</Button>
      </FieldRow>
      {failure !== null && <p className="ag-field__error" role="alert">{errorText(failure, "error.generic")}</p>}
    </ConfirmDialog>
  );
}

function OwnerDialog({ row, onClose }: { row: Schedule; onClose: () => void }) {
  const t = useT("scheduler");
  const errorText = useErrorText("scheduler");
  const qc = useQueryClient();
  const users = useUsers(row.id);
  const [pick, setPick] = useState("");
  const change = useMutation({
    mutationFn: (userId: number) => apiFetch(`${scheduleUrl(row.id)}/owner`, { method: "POST", json: { user_id: userId } }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["scheduler"] }); onClose(); },
  });
  return (
    <ConfirmDialog opened title={t("owner.title")} confirmLabel={t("owner.confirm")} busy={!pick || change.isPending}
      onCancel={onClose} onConfirm={() => change.mutate(Number(pick))}>
      <p>{t("owner.text")}</p>
      <SelectField label={t("owner.label")} name="owner" searchable placeholder={t("owner.placeholder")} value={pick} onChange={setPick}
        options={(users.data ?? []).map((u) => ({ value: String(u.id), label: u.username }))} />
      {change.error && <p className="ag-field__error" role="alert">{errorText(change.error, "error.generic")}</p>}
    </ConfirmDialog>
  );
}
