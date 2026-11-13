// The Custom recurrence dialog (PRD D-16): repeat every N days/weeks/months/years at a time of
// day, on weekdays (week) or a day of the month / the Nth weekday (month), until never / a date /
// N runs. It only writes an RRULE; the server checks it and the form previews it.
import { useState } from "react";
import { ConfirmDialog, SelectField, TextField } from "@agento/ui";

export interface Spec {
  freq: "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY"; interval: number; time: string; days: string[];
  monthly: "day" | "nth"; monthDay: number; nth: number; nthDay: string;
  ends: "never" | "on" | "after"; until: string; count: number;
}

const WEEKDAYS = [["MO", "Monday"], ["TU", "Tuesday"], ["WE", "Wednesday"], ["TH", "Thursday"], ["FR", "Friday"],
  ["SA", "Saturday"], ["SU", "Sunday"]] as const;
const FREQS = [{ value: "DAILY", label: "days" }, { value: "WEEKLY", label: "weeks" }, { value: "MONTHLY", label: "months" },
  { value: "YEARLY", label: "years" }];
const NTH = [{ value: "1", label: "first" }, { value: "2", label: "second" }, { value: "3", label: "third" },
  { value: "4", label: "fourth" }, { value: "-1", label: "last" }];
const INITIAL: Spec = {
  freq: "WEEKLY", interval: 1, time: "09:00", days: [], monthly: "day", monthDay: 1, nth: 1, nthDay: "MO",
  ends: "never", until: "", count: 10,
};

/** The RRULE of a spec. UNTIL is naive local (end of that day in the schedule's zone), as the engine reads it. */
export function buildRrule(s: Spec): string {
  const [hour, minute] = s.time.split(":").map(Number);
  const parts = [`FREQ=${s.freq}`];
  if (s.interval > 1) parts.push(`INTERVAL=${s.interval}`);
  if (s.freq === "WEEKLY" && s.days.length) parts.push(`BYDAY=${WEEKDAYS.map(([d]) => d).filter((d) => s.days.includes(d)).join(",")}`);
  if (s.freq === "MONTHLY") parts.push(s.monthly === "day" ? `BYMONTHDAY=${s.monthDay}` : `BYDAY=${s.nth}${s.nthDay}`);
  parts.push(`BYHOUR=${hour}`, `BYMINUTE=${minute}`);
  if (s.ends === "on") parts.push(`UNTIL=${s.until.replaceAll("-", "")}T235959`);
  if (s.ends === "after") parts.push(`COUNT=${s.count}`);
  return parts.join(";");
}

const valid = (s: Spec) => s.interval >= 1 && /^\d{2}:\d{2}$/.test(s.time) && (s.ends !== "on" || s.until !== "")
  && (s.ends !== "after" || s.count >= 1) && (s.freq !== "MONTHLY" || s.monthly === "nth" || (s.monthDay >= 1 && s.monthDay <= 31));

export function RecurrenceDialog({ opened, onDone, onCancel }: { opened: boolean; onDone: (rrule: string) => void; onCancel: () => void }) {
  const [s, setS] = useState(INITIAL);
  const set = (patch: Partial<Spec>) => setS({ ...s, ...patch });
  const toggle = (day: string) => set({ days: s.days.includes(day) ? s.days.filter((d) => d !== day) : [...s.days, day] });
  return (
    <ConfirmDialog opened={opened} title="Custom recurrence" confirmLabel="Done" busy={!valid(s)}
      onCancel={onCancel} onConfirm={() => onDone(buildRrule(s))}>
      <div className="ag-row">
        <TextField label="Every" type="number" min={1} value={String(s.interval)} onChange={(e) => set({ interval: Number(e.target.value) })} />
        <SelectField label="Repeat" name="freq" value={s.freq} options={FREQS} onChange={(v) => set({ freq: v as Spec["freq"] })} />
        <TextField label="Time" type="time" value={s.time} onChange={(e) => set({ time: e.target.value })} />
      </div>
      {s.freq === "WEEKLY" && (
        <fieldset className="ag-row">
          <legend className="ag-field__label">On</legend>
          {WEEKDAYS.map(([day, name]) => (
            <label key={day}><input type="checkbox" aria-label={name} checked={s.days.includes(day)} onChange={() => toggle(day)} /> {day}</label>
          ))}
        </fieldset>
      )}
      {s.freq === "MONTHLY" && (
        <fieldset className="ag-stack">
          <legend className="ag-field__label">On</legend>
          <label className="ag-row">
            <input type="radio" name="monthly" checked={s.monthly === "day"} onChange={() => set({ monthly: "day" })} /> Day of the month
          </label>
          {s.monthly === "day" && (
            <TextField label="Day" type="number" min={1} max={31} value={String(s.monthDay)} onChange={(e) => set({ monthDay: Number(e.target.value) })} />
          )}
          <label className="ag-row">
            <input type="radio" name="monthly" checked={s.monthly === "nth"} onChange={() => set({ monthly: "nth" })} /> A weekday of the month
          </label>
          {s.monthly === "nth" && (
            <div className="ag-row">
              <SelectField label="Which" name="nth" value={String(s.nth)} options={NTH} onChange={(v) => set({ nth: Number(v) })} />
              <SelectField label="Weekday" name="nth-day" value={s.nthDay} options={WEEKDAYS.map(([value, label]) => ({ value, label }))}
                onChange={(v) => set({ nthDay: v })} />
            </div>
          )}
        </fieldset>
      )}
      <div className="ag-row">
        <SelectField label="Ends" name="ends" value={s.ends} onChange={(v) => set({ ends: v as Spec["ends"] })}
          options={[{ value: "never", label: "Never" }, { value: "on", label: "On a date" }, { value: "after", label: "After a number of runs" }]} />
        {s.ends === "on" && <TextField label="Last day" type="date" value={s.until} onChange={(e) => set({ until: e.target.value })} />}
        {s.ends === "after" && (
          <TextField label="Runs" type="number" min={1} value={String(s.count)} onChange={(e) => set({ count: Number(e.target.value) })} />
        )}
      </div>
    </ConfirmDialog>
  );
}
