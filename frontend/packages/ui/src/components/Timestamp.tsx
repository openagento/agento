/** A machine-readable <time> with a locale label. An unparseable value is shown as is. */
export function Timestamp({ value }: { value: string }) {
  const d = new Date(value);
  const label = Number.isNaN(d.getTime()) ? value : d.toLocaleString();
  return <time dateTime={value}>{label}</time>;
}
