// The tool-risk vocabulary (PRD §4–§5): the three risk labels, the icon of every label, the review
// state and the lethal-trifecta warning. One file, so the admin Tools screen and the conversation
// screen show a risk alike. Panel only: tool labels are admin data, so there is no .ag-* twin (UI-2).
import type { ReactNode } from "react";
import { Alert, Anchor, Badge, Group, Stack, Text } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { Check, CircleDot, FlaskConical, LockKeyhole, Send, ShieldAlert, Tag, TriangleAlert, type LucideIcon } from "lucide-react";
import { useT } from "../i18n";
import classes from "./ToolRisk.module.css";

export const RISK_KEYS = ["private-data", "untrusted-content", "external-communication"] as const;
export type RiskKey = (typeof RISK_KEYS)[number];
/** The names of the three risk labels, in the panel's language. */
export function useRiskNames(): Record<RiskKey, string> {
  const t = useT("ui");
  return {
    "private-data": t("risk.private_data"),
    "untrusted-content": t("risk.untrusted_content"),
    "external-communication": t("risk.external_communication"),
  };
}

// Maps, not object lookups: a label key is admin data, and "constructor" must not find Object.
const ICONS = new Map<string, LucideIcon>([
  ["private-data", LockKeyhole], ["untrusted-content", TriangleAlert], ["external-communication", Send], ["stub-in-test", FlaskConical],
]);
const HUES = new Map(RISK_KEYS.map((k) => [k as string, classes[k]]));

/** A label's icon: its shape names the label, a risk label's hue repeats it. A custom label is a tag. */
export function labelIcon(labelKey: string, size = 12, inAlert = false): ReactNode {
  const Icon = ICONS.get(labelKey) ?? Tag;
  return <Icon size={size} aria-hidden data-risk={labelKey}
    className={`${classes.icon} ${HUES.get(labelKey) ?? ""} ${inAlert ? classes.inAlert : ""}`} />;
}

/** A long label name (up to 100 characters) wraps inside the badge instead of being cut. */
export function LabelBadge({ labelKey, name }: { labelKey: string; name: string }) {
  return (
    <Badge variant="default" size="sm" tt="none" fw={500} leftSection={labelIcon(labelKey)}
      classNames={{ root: classes.badge, label: classes.badgeLabel }}>{name}</Badge>
  );
}

/** One look for a tool's review state: an icon and a word, no hue. A fresh install has every tool to
 *  review, so a hue here would be a wall of one color; the done state is dimmed. */
export function ReviewState({ reviewed }: { reviewed: boolean }) {
  const t = useT("ui");
  return (
    <Group gap="xs" wrap="nowrap">
      {reviewed
        ? <Check size={14} aria-hidden className={classes.icon} />
        : <CircleDot size={14} aria-hidden />}
      <Text size="sm" c={reviewed ? "dimmed" : undefined}>{reviewed ? t("risk.reviewed") : t("risk.to_review")}</Text>
    </Group>
  );
}

function Groups({ categories }: { categories: Partial<Record<RiskKey, string[]>> }) {
  const names = useRiskNames();
  return (
    <ul className={classes.groups}>
      {RISK_KEYS.map((k) => (
        <li key={k} className={classes.group}>
          {labelIcon(k, 14, true)}
          <Text size="sm"><Text span size="sm" fw={600}>{names[k]}:</Text> {(categories[k] ?? []).join(", ")}</Text>
        </li>
      ))}
    </ul>
  );
}

/** The red lethal-trifecta warning. `compact`: a repeat of an earlier warning, one line with the
 *  same groups behind "Show tools". It stays red: a trifecta is never shown quietly. */
export function TrifectaAlert({ title, intro, categories, compact }: {
  title: string; intro: ReactNode; categories: Partial<Record<RiskKey, string[]>>; compact?: ReactNode;
}) {
  const t = useT("ui");
  const [open, { toggle }] = useDisclosure(false);
  const full = (
    <Stack gap="xs">
      <Text size="sm" component="div">{intro}</Text>
      <Groups categories={categories} />
    </Stack>
  );
  return (
    <Alert color="red" variant="light" title={title} icon={<ShieldAlert size={18} />}>
      {compact === undefined ? full : (
        <Stack gap="xs">
          <Group gap="xs" justify="space-between" wrap="nowrap">
            <Text size="sm">{compact}</Text>
            {/* The alert's own text color, underlined: no new color pair on the red face, hovered or not. */}
            <Anchor component="button" type="button" size="sm" c="inherit" underline="always" className={classes.disclosure}
              aria-expanded={open} onClick={toggle}>
              {open ? t("risk.hide_tools") : t("risk.show_tools")}
            </Anchor>
          </Group>
          {open && full}
        </Stack>
      )}
    </Alert>
  );
}
