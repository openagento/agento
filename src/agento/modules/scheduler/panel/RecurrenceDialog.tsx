// The Custom recurrence dialog (PRD D-16): repeat every N days/weeks/months/years at a time of
// day, on weekdays (week) or a day of the month / the Nth weekday (month), until never / a date /
// N runs. It only writes an RRULE; the server checks it and the form previews it.
import { useState } from "react";
import { ConfirmDialog, dateTimeFormat, FieldRow, SelectField, TextField, useLocale, useT, type Translate } from "@agento/ui";

export interface Spec {
  freq: "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY"; interval: number; time: string; days: string[];
  monthly: "day" | "nth"; monthDay: number; nth: number; nthDay: string;
  ends: "never" | "on" | "after"; until: string; count: number;
}

const DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"] as const;
/** 2024-01-01 is a Monday: the full name of each RRULE day in the panel's language. */
const dayNames = (locale: string) => {
  const name = dateTimeFormat(locale, { weekday: "long", timeZone: "UTC" });
  return DAYS.map((_, i) => name.format(Date.UTC(2024, 0, 1 + i)));
};
const weekdays = (t: Translate, locale: string) => {
  const short = [t("custom.day_short.MO"), t("custom.day_short.TU"), t("custom.day_short.WE"), t("custom.day_short.TH"),
    t("custom.day_short.FR"), t("custom.day_short.SA"), t("custom.day_short.SU")];
  const names = dayNames(locale);
  return DAYS.map((day, i) => ({ day, name: names[i], short: short[i] }));
};
const freqs = (t: Translate) => [{ value: "DAILY", label: t("custom.freq.days") }, { value: "WEEKLY", label: t("custom.freq.weeks") },
  { value: "MONTHLY", label: t("custom.freq.months") }, { value: "YEARLY", label: t("custom.freq.years") }];
const nths = (t: Translate) => [{ value: "1", label: t("custom.nth.first") }, { value: "2", label: t("custom.nth.second") },
  { value: "3", label: t("custom.nth.third") }, { value: "4", label: t("custom.nth.fourth") }, { value: "-1", label: t("custom.nth.last") }];
const INITIAL: Spec = {
  freq: "WEEKLY", interval: 1, time: "09:00", days: [], monthly: "day", monthDay: 1, nth: 1, nthDay: "MO",
  ends: "never", until: "", count: 10,
};

/** The RRULE of a spec. UNTIL is naive local (end of that day in the schedule's zone), as the engine reads it. */
export function buildRrule(s: Spec): string {
  const [hour, minute] = s.time.split(":").map(Number);
  const parts = [`FREQ=${s.freq}`];
  if (s.interval > 1) parts.push(`INTERVAL=${s.interval}`);
  if (s.freq === "WEEKLY" && s.days.length) parts.push(`BYDAY=${DAYS.filter((d) => s.days.includes(d)).join(",")}`);
  if (s.freq === "MONTHLY") parts.push(s.monthly === "day" ? `BYMONTHDAY=${s.monthDay}` : `BYDAY=${s.nth}${s.nthDay}`);
  parts.push(`BYHOUR=${hour}`, `BYMINUTE=${minute}`);
  if (s.ends === "on") parts.push(`UNTIL=${s.until.replaceAll("-", "")}T235959`);
  if (s.ends === "after") parts.push(`COUNT=${s.count}`);
  return parts.join(";");
}

const valid = (s: Spec) => s.interval >= 1 && /^\d{2}:\d{2}$/.test(s.time) && (s.ends !== "on" || s.until !== "")
  && (s.ends !== "after" || s.count >= 1) && (s.freq !== "MONTHLY" || s.monthly === "nth" || (s.monthDay >= 1 && s.monthDay <= 31));

export function RecurrenceDialog({ opened, onDone, onCancel }: { opened: boolean; onDone: (rrule: string) => void; onCancel: () => void }) {
  const t = useT("scheduler");
  const days = weekdays(t, useLocale());
  const [s, setS] = useState(INITIAL);
  const set = (patch: Partial<Spec>) => setS({ ...s, ...patch });
  const toggle = (day: string) => set({ days: s.days.includes(day) ? s.days.filter((d) => d !== day) : [...s.days, day] });
  return (
    <ConfirmDialog opened={opened} title={t("custom.title")} confirmLabel={t("custom.done")} busy={!valid(s)}
      onCancel={onCancel} onConfirm={() => onDone(buildRrule(s))}>
      <FieldRow>
        <TextField label={t("custom.every")} type="number" min={1} value={String(s.interval)} onChange={(e) => set({ interval: Number(e.target.value) })} />
        <SelectField label={t("custom.repeat")} name="freq" value={s.freq} options={freqs(t)} onChange={(v) => set({ freq: v as Spec["freq"] })} />
        <TextField label={t("custom.time")} type="time" value={s.time} onChange={(e) => set({ time: e.target.value })} />
      </FieldRow>
      {s.freq === "WEEKLY" && (
        <fieldset className="ag-row">
          <legend className="ag-field__label">{t("custom.on")}</legend>
          {days.map(({ day, name, short }) => (
            <label key={day}><input type="checkbox" aria-label={name} checked={s.days.includes(day)} onChange={() => toggle(day)} /> {short}</label>
          ))}
        </fieldset>
      )}
      {s.freq === "MONTHLY" && (
        <fieldset className="ag-stack">
          <legend className="ag-field__label">{t("custom.on")}</legend>
          <label className="ag-row">
            <input type="radio" name="monthly" checked={s.monthly === "day"} onChange={() => set({ monthly: "day" })} /> {t("custom.month_day")}
          </label>
          {s.monthly === "day" && (
            <TextField label={t("custom.day")} type="number" min={1} max={31} value={String(s.monthDay)} onChange={(e) => set({ monthDay: Number(e.target.value) })} />
          )}
          <label className="ag-row">
            <input type="radio" name="monthly" checked={s.monthly === "nth"} onChange={() => set({ monthly: "nth" })} /> {t("custom.month_weekday")}
          </label>
          {s.monthly === "nth" && (
            <FieldRow>
              <SelectField label={t("custom.which")} name="nth" value={String(s.nth)} options={nths(t)} onChange={(v) => set({ nth: Number(v) })} />
              <SelectField label={t("custom.weekday")} name="nth-day" value={s.nthDay} options={days.map(({ day, name }) => ({ value: day, label: name }))}
                onChange={(v) => set({ nthDay: v })} />
            </FieldRow>
          )}
        </fieldset>
      )}
      <FieldRow>
        <SelectField label={t("custom.ends")} name="ends" value={s.ends} onChange={(v) => set({ ends: v as Spec["ends"] })}
          options={[{ value: "never", label: t("custom.ends.never") }, { value: "on", label: t("custom.ends.on") }, { value: "after", label: t("custom.ends.after") }]} />
        {s.ends === "on" && <TextField label={t("custom.last_day")} type="date" value={s.until} onChange={(e) => set({ until: e.target.value })} />}
        {s.ends === "after" && (
          <TextField label={t("custom.runs")} type="number" min={1} value={String(s.count)} onChange={(e) => set({ count: Number(e.target.value) })} />
        )}
      </FieldRow>
    </ConfirmDialog>
  );
}
