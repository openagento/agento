// The architectural acceptance component (PRD E8 §10.3): one implementation, rendered in
// the panel, in Storybook and — as the same markup over agento-ui.css — in a miniapp.
// It emits only `.ag-*` classes; its whole look comes from the shared stylesheet.
import { useT, withElements, type Translate } from "../i18n";
import { Timestamp } from "./Timestamp";

export type JobState = "pending" | "published" | "running" | "succeeded" | "failed" | "blocked";

export const jobStateLabels = (t: Translate): Record<JobState, string> => ({
  pending: t("job_state.pending"),
  published: t("job_state.published"),
  running: t("job_state.running"),
  succeeded: t("job_state.succeeded"),
  failed: t("job_state.failed"),
  blocked: t("job_state.blocked"),
});

export function JobStatusCard({ title, state, detail, updatedAt }: {
  title: string; state: JobState; detail?: string; updatedAt?: string;
}) {
  const t = useT("ui");
  return (
    <article className="ag-card ag-job">
      <header className="ag-card__head">
        <h3 className="ag-card__title">{title}</h3>
        <span className={`ag-badge ag-badge--${state}`}>{jobStateLabels(t)[state]}</span>
      </header>
      {detail && <p className="ag-card__body">{detail}</p>}
      {updatedAt && <p className="ag-card__meta">{withElements(t("job_card.updated"), { time: <Timestamp value={updatedAt} /> })}</p>}
    </article>
  );
}
