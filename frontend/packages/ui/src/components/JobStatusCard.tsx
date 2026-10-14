// The architectural acceptance component (PRD E8 §10.3): one implementation, rendered in
// the panel, in Storybook and — as the same markup over agento-ui.css — in a miniapp.
// It emits only `.ag-*` classes; its whole look comes from the shared stylesheet.
import { Timestamp } from "./Timestamp";

export type JobState = "pending" | "published" | "running" | "succeeded" | "failed" | "blocked";

export const JOB_STATE_LABEL: Record<JobState, string> = {
  pending: "Pending",
  published: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  blocked: "Blocked",
};

export function JobStatusCard({ title, state, detail, updatedAt }: {
  title: string; state: JobState; detail?: string; updatedAt?: string;
}) {
  return (
    <article className="ag-card ag-job">
      <header className="ag-card__head">
        <h3 className="ag-card__title">{title}</h3>
        <span className={`ag-badge ag-badge--${state}`}>{JOB_STATE_LABEL[state]}</span>
      </header>
      {detail && <p className="ag-card__body">{detail}</p>}
      {updatedAt && <p className="ag-card__meta">Updated <Timestamp value={updatedAt} /></p>}
    </article>
  );
}
