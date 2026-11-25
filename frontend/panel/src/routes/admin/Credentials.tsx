import { useEffect, useRef, useState, type ReactNode } from "react";
import { Ban, ChartColumn, Eraser, LogIn } from "lucide-react";
import { Anchor, Drawer, Group, Modal, Progress, Stack, Table, Text, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useErrorText, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  Button, ConfirmDialog, CopyButton, DataTable, IconAction, numberFormat, PageHeader, StatusBadge, TextField, Timestamp, useFormat, useLocale, useLookup, useT, withElements,
  type Column, type Translate,
} from "@agento/ui";
import css from "./Credentials.module.css";
import { whole } from "./shared";

interface LimitWindow { label: string; used_pct: number; resets_at: string | null }
interface Limits { windows: LimitWindow[]; balance_usd: number | null }

// An allow-list on the server: no token and no error_msg ever arrives here (SEC-6).
export interface Credential {
  id: number; scope: string; label: string; status: string; enabled: boolean; type: string;
  error_source: "auto" | "operator" | null; used_at: string | null; expires_at: string | null;
  token_limit: number; tokens_used: number; call_count: number; pct_free: number;
  limits: Limits | null; limits_at: string | null;
}

/** The error state is `status`, as in the TUI; a stale message on an ok credential is not an error. */
export const inError = (c: Pick<Credential, "status">) => c.status === "error";
export const credentialBadge = (t: Translate, c: Pick<Credential, "status" | "enabled">) => !c.enabled
  ? <StatusBadge tone="neutral">{t("credentials.status.disabled")}</StatusBadge>
  : <StatusBadge tone={inError(c) ? "failed" : "succeeded"}>{inError(c) ? t("credentials.status.error") : t("credentials.status.ok")}</StatusBadge>;
export const freeColor = (pct: number) => (pct >= 50 ? "green" : pct >= 20 ? "yellow" : "red");
const when = (v: string | null) => (v ? <Timestamp value={v} /> : "—");
const withTime = (sentence: string, value: string) => withElements(sentence, { time: <Timestamp value={value} /> });
const usd = (locale: string, amount: number) => numberFormat(locale, { style: "currency", currency: "USD" }).format(amount);
const freeOf = (w: LimitWindow) => Math.round(100 - w.used_pct);
/** The smallest free share of the vendor windows; a credential with none sorts first. */
const minFree = (c: Credential) => {
  const w = c.limits?.windows ?? [];
  return w.length ? Math.min(...w.map(freeOf)) : -1;
};

const LIMITS_TICK_MS = 60_000;
/** Limits not read for 5 cron ticks: the cron could not renew or read this credential. */
export const isStale = (limitsAt: string | null, now = Date.now()) =>
  limitsAt !== null && now - Date.parse(limitsAt) > 5 * LIMITS_TICK_MS;

function FreeCell({ c }: { c: Credential }) {
  const t = useT("panel");
  const locale = useLocale();
  const { percent } = useFormat();
  const lookup = useLookup();
  const windows = (c.limits?.windows ?? []).map((w) => ({ ...w, id: w.label, name: lookup(c.scope, `limit.${w.label}`) ?? w.label }));
  const stale = c.limits != null && c.limits_at !== null && isStale(c.limits_at) && (
    <Tooltip label={withTime(t("credentials.stale_hint"), c.limits_at)}>
      <Text size="xs" c="dimmed">{t("credentials.stale")}</Text>
    </Tooltip>
  );
  if (windows.length) return (
    <Stack gap={2}>
      {stale}
      {windows.map((w) => (
        <Tooltip key={w.id} label={w.resets_at ? withTime(t("credentials.resets"), w.resets_at) : t("credentials.reset_unknown")}>
          <Group gap="xs" wrap="nowrap">
            <Text size="xs" w={40}>{w.name}</Text>
            <Progress value={freeOf(w)} color={freeColor(freeOf(w))} size="sm" w={80} aria-label={t("credentials.free_label", { credential: c.label, window: w.name })} />
            <Text size="xs">{percent(freeOf(w))}</Text>
          </Group>
        </Tooltip>
      ))}
    </Stack>
  );
  if (c.limits?.balance_usd != null) return (
    <Group gap="xs" wrap="nowrap"><Text size="sm">{usd(locale, c.limits.balance_usd)}</Text>{stale}</Group>
  );
  // `limits_at` with no limits: the check ran and failed (the cron log names the HTTP status).
  if (c.limits == null && c.limits_at) return (
    <Tooltip label={withTime(t("credentials.check_failed_hint"), c.limits_at)}>
      <Text size="xs" c="dimmed">{t("credentials.check_failed")}</Text>
    </Tooltip>
  );
  return "—";
}

interface Login {
  status: "pending" | "starting" | "waiting" | "verifying" | "done" | "failed" | "cancelled";
  verify_url: string | null; user_code: string | null; needs_code: boolean; error_code: string | null; expires_at: string;
}
const ACTIVE = new Set<Login["status"]>(["pending", "starting", "waiting", "verifying"]);
/** `error_code` is a fixed word from the worker; CLI output never reaches the panel (SEC-6). */
const loginErrors = (t: Translate): Record<string, string> => ({
  expired: t("credentials.login.error.expired"),
  cli_failed: t("credentials.login.error.cli_failed"),
  bad_url: t("credentials.login.error.bad_url"),
  disabled: t("credentials.login.error.disabled"),
  busy: t("credentials.login.error.busy"),
  unsupported: t("credentials.login.error.unsupported"),
  abandoned: t("credentials.login.error.abandoned"),
});
const loginFailure = (t: Translate, l: Login) => l.status === "cancelled"
  ? t("credentials.login.cancelled") : loginErrors(t)[l.error_code ?? ""] ?? t("credentials.login.failed");
// The server takes 1–300 printable ASCII characters with no space (RSA-OAEP seals at most 318 bytes).
const CODE = /^[\x21-\x7e]{1,300}$/;

function CodeForm({ loginId }: { loginId: number }) {
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const [code, setCode] = useState("");
  const [invalid, setInvalid] = useState(false);
  const send = useMutation({
    mutationFn: (value: string) => apiFetch(`/api/admin/credential-logins/${loginId}/code`, { method: "POST", json: { code: value } }),
  });
  if (send.isSuccess) return <Text>{t("credentials.login.checking")}</Text>;
  const save = () => {
    const value = code.trim();
    setInvalid(!CODE.test(value));
    if (CODE.test(value)) send.mutate(value);
    else send.reset();
  };
  const error = invalid ? t("credentials.login.code_invalid")
    : send.error ? errorText(send.error, "credentials.login.code_failed") : null;
  return (
    <form className="ag-stack" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField label={t("credentials.login.code_label")} value={code} error={error} autoComplete="off"
        onChange={(e) => setCode(e.currentTarget.value)} />
      <Group><Button type="submit" variant="primary" disabled={send.isPending}>{t("action.save")}</Button></Group>
    </form>
  );
}

function LoginModal({ c, loginId, startError, onRetry, onClose }: {
  c: Credential | null; loginId: number | null; startError: string | null; onRetry: () => void; onClose: () => void;
}) {
  const qc = useQueryClient();
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const poll = useQuery({
    queryKey: ["credential-login", loginId],
    queryFn: ({ signal }) => apiFetch<Login>(`/api/admin/credential-logins/${loginId}`, { signal }),
    enabled: loginId !== null,
    refetchInterval: (q) => (q.state.data && !ACTIVE.has(q.state.data.status) ? false : 2000),
  });
  const l = loginId === null ? undefined : poll.data;
  useEffect(() => {
    if (l?.status === "done") void qc.invalidateQueries({ queryKey: ["admin-credentials"] });
  }, [l?.status, qc]);
  const close = () => {
    // A login still running holds the CLI and blocks a new one: closing cancels it. No answer yet = running.
    if (loginId !== null && (!l || ACTIVE.has(l.status))) {
      apiFetch(`/api/admin/credential-logins/${loginId}/cancel`, { method: "POST" })
        .catch((e) => notifications.show({ color: "red", message: errorText(e, "credentials.login.cancel_failed") }));
    }
    // The limits are read just after the login commits (core observer), so read the list again.
    if (l?.status === "done") void qc.invalidateQueries({ queryKey: ["admin-credentials"] });
    onClose();
  };
  const retry = <Group><Button variant="primary" onClick={onRetry}>{t("credentials.login.retry")}</Button></Group>;
  let body: ReactNode;
  if (startError) body = <><Text>{startError}</Text>{retry}</>;
  else if (!l || l.status === "pending" || l.status === "starting") body = <Text>{t("credentials.login.starting")}</Text>;
  else if (l.status === "waiting") body = (
    <>
      {l.verify_url && (
        <Anchor href={l.verify_url} target="_blank" rel="noopener noreferrer">{t("credentials.login.open_page")}</Anchor>
      )}
      {l.user_code && (
        <Group gap="xs"><Text>{t("credentials.login.enter_code")}</Text><code>{l.user_code}</code><CopyButton value={l.user_code} /></Group>
      )}
      {l.needs_code && loginId !== null && <CodeForm key={loginId} loginId={loginId} />}
    </>
  );
  else if (l.status === "verifying") body = <Text>{t("credentials.login.checking")}</Text>;
  else if (l.status === "done") body = (
    <div className={css.done} role="status">
      <svg className={css.mark} viewBox="0 0 52 52" aria-hidden="true">
        <circle cx="26" cy="26" r="23" pathLength={1} />
        <path d="M15 27l7 7 15-15" pathLength={1} />
      </svg>
      <Text>{t("credentials.login.done")}</Text>
    </div>
  );
  else body = <><Text>{loginFailure(t, l)}</Text>{retry}</>;
  return (
    <Modal opened={c !== null} onClose={close} title={c ? t("credentials.login.title", { label: c.label }) : ""} centered
      closeButtonProps={{ "aria-label": t("action.close") }}>
      <div className="ag-stack">
        {poll.error && <Text c="red">{errorText(poll.error, "credentials.login.status_failed")}</Text>}
        {body}
      </div>
    </Modal>
  );
}

type Action = { kind: "clear-error" | "disable"; credential: Credential };

function UsageDrawer({ c, onClose }: { c: Credential | null; onClose: () => void }) {
  const t = useT("panel");
  const locale = useLocale();
  const { number, percent } = useFormat();
  const lookup = useLookup();
  const facts: { id: string; label: string; value: ReactNode }[] = c ? [
    { id: "scope", label: t("credentials.column.scope"), value: c.scope },
    { id: "label", label: t("credentials.column.label"), value: c.label },
    { id: "status", label: t("credentials.column.status"), value: credentialBadge(t, c) },
    { id: "used", label: t("credentials.column.used"), value: when(c.used_at) },
    { id: "expires", label: t("credentials.column.expires"), value: when(c.expires_at) },
    { id: "token_limit", label: t("credentials.fact.token_limit"), value: c.token_limit ? number(c.token_limit) : t("credentials.fact.token_limit_none") },
    { id: "tokens_used", label: t("credentials.fact.tokens_used"), value: number(c.tokens_used) },
    { id: "calls", label: t("credentials.fact.calls"), value: number(c.call_count) },
    { id: "local_limit", label: t("credentials.fact.local_limit"), value: percent(c.pct_free) },
    ...(c.limits?.windows ?? []).map((w) => ({ id: `window:${w.label}`, label: lookup(c.scope, `limit.${w.label}`) ?? w.label, value: w.resets_at
      ? withTime(t("credentials.window_free", { pct: percent(freeOf(w)) }), w.resets_at)
      : t("credentials.window_free_unknown", { pct: percent(freeOf(w)) }) })),
    ...(c.limits?.balance_usd != null ? [{ id: "balance", label: t("credentials.fact.balance"), value: usd(locale, c.limits.balance_usd) }] : []),
    { id: "checked", label: t("credentials.fact.checked"), value: when(c.limits_at) },
  ] : [];
  return (
    <Drawer opened={c !== null} onClose={onClose} position="right" title={c ? t("credentials.drawer_title", { label: c.label }) : ""}
      closeButtonProps={{ "aria-label": t("action.close") }}>
      {c && (
        <>
          <Table>
            <Table.Tbody>
              {facts.map((f) => <Table.Tr key={f.id}><Table.Th scope="row">{f.label}</Table.Th><Table.Td>{f.value}</Table.Td></Table.Tr>)}
            </Table.Tbody>
          </Table>
          {inError(c) && (
            <Text size="sm" c="dimmed" mt="md">
              {c.error_source === "operator" ? t("credentials.error_source.operator") : t("credentials.error_source.auto")} {t("credentials.error_hint")}
            </Text>
          )}
        </>
      )}
    </Drawer>
  );
}

export function Credentials() {
  const qc = useQueryClient();
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const { number } = useFormat();
  const creds = useQuery({
    queryKey: ["admin-credentials"],
    queryFn: ({ signal }) => apiFetch<Credential[]>("/api/admin/credentials", { signal }),
    refetchInterval: LIMITS_TICK_MS, // credential:limits stores new limits every minute
  });
  const [action, setAction] = useState<Action | null>(null);
  const [detail, setDetail] = useState<Credential | null>(null);
  const [relogin, setRelogin] = useState<Credential | null>(null);
  // Each opening of the login modal is one attempt; a credential id alone does not tell a closed
  // attempt from a reopened one.
  const attempts = useRef(0);
  const open = useRef<number | null>(null); // the attempt whose login modal is open
  const start = useMutation({
    mutationFn: ({ c }: { c: Credential; attempt: number }) =>
      apiFetch<{ id: number }>(`/api/admin/credentials/${c.id}/login`, { method: "POST" }),
    // Closed before the start answered: the login would hold the CLI until it expires.
    onSuccess: (r, v) => {
      if (open.current !== v.attempt) void apiFetch(`/api/admin/credential-logins/${r.id}/cancel`, { method: "POST" }).catch(() => undefined);
    },
  });
  const beginLogin = (c: Credential) => {
    open.current = ++attempts.current;
    setRelogin(c);
    start.mutate({ c, attempt: open.current });
  };
  const run = useMutation({
    mutationFn: (a: Action) => apiFetch(`/api/admin/credentials/${a.credential.id}/${a.kind}`, { method: "POST" }),
    onSuccess: (_r, a) => {
      notifications.show({ message: a.kind === "disable"
        ? t("credentials.disabled", { label: a.credential.label }) : t("credentials.error_cleared", { label: a.credential.label }) });
      setAction(null);
      void qc.invalidateQueries({ queryKey: ["admin-credentials"] });
    },
    onError: (e) => notifications.show({ color: "red", message: errorText(e, "credentials.action_failed") }),
  });

  const columns: Column<Credential>[] = [
    { id: "scope", header: t("credentials.column.scope"), cell: (c) => <code>{c.scope}</code>, sortValue: (c) => c.scope },
    { id: "label", header: t("credentials.column.label"), cell: (c) => c.label, sortValue: (c) => c.label },
    { id: "status", header: t("credentials.column.status"), cell: (c) => whole(credentialBadge(t, c)), sortValue: (c) => c.status },
    { id: "used", header: t("credentials.column.used"), cell: (c) => when(c.used_at), sortValue: (c) => c.used_at ?? "" },
    { id: "expires", header: t("credentials.column.expires"), cell: (c) => when(c.expires_at), sortValue: (c) => c.expires_at ?? "" },
    { id: "used24", header: t("credentials.column.used24"), cell: (c) => number(c.tokens_used), sortValue: (c) => c.tokens_used },
    { id: "free", header: t("credentials.column.free"), sortValue: minFree, cell: (c) => <FreeCell c={c} /> },
    { id: "actions", header: t("credentials.column.actions"), cell: (c) => (
      <Group gap="xs" wrap="nowrap">
        <IconAction label={t("credentials.usage")} icon={<ChartColumn size={16} />} onClick={() => setDetail(c)} />
        {c.enabled && c.type === "oauth" && <IconAction label={t("credentials.relogin")} icon={<LogIn size={16} />} onClick={() => beginLogin(c)} />}
        {inError(c) && <IconAction label={t("credentials.clear_error")} icon={<Eraser size={16} />} onClick={() => setAction({ kind: "clear-error", credential: c })} />}
        {c.enabled && <IconAction danger label={t("credentials.disable")} icon={<Ban size={16} />} onClick={() => setAction({ kind: "disable", credential: c })} />}
      </Group>
    ) },
  ];

  return (
    <div className="ag-stack">
      <PageHeader title={t("credentials.title")} description={t("credentials.description")} />
      <DataTable caption={t("credentials.title")} columns={columns} rows={creds.data} rowKey={(c) => String(c.id)}
        onRowActivate={setDetail} loading={creds.isPending} error={creds.error ? errorText(creds.error, "credentials.load_failed") : null}
        onRetry={() => void creds.refetch()} emptyTitle={t("credentials.empty")} />
      {creds.data?.some(inError) && <Text size="sm" c="dimmed">{t("credentials.error_hint")}</Text>}
      <ConfirmDialog opened={action !== null} danger={action?.kind === "disable"} busy={run.isPending}
        title={action?.kind === "disable" ? t("credentials.disable_confirm.title") : t("credentials.clear_confirm.title")}
        confirmLabel={action?.kind === "disable" ? t("credentials.disable") : t("credentials.clear_error")}
        onCancel={() => setAction(null)} onConfirm={() => action && run.mutate(action)}>
        <p>{action?.kind === "disable"
          ? t("credentials.disable_confirm.text", { label: action.credential.label })
          : t("credentials.clear_confirm.text", { label: action?.credential.label ?? "" })}</p>
      </ConfirmDialog>
      <UsageDrawer c={detail} onClose={() => setDetail(null)} />
      <LoginModal c={relogin} loginId={start.data?.id ?? null} startError={start.error ? errorText(start.error, "credentials.login.start_failed") : null}
        onRetry={() => relogin && beginLogin(relogin)}
        onClose={() => { open.current = null; setRelogin(null); start.reset(); }} />
    </div>
  );
}
