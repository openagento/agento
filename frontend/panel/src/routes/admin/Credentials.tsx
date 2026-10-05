import { useEffect, useRef, useState, type ReactNode } from "react";
import { Anchor, Drawer, Group, Modal, Progress, Stack, Table, Text, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useMutation, useQuery, useQueryClient } from "@agento/api";
import {
  Button, ConfirmDialog, CopyButton, DataTable, PageHeader, StatusBadge, TextField, Timestamp, type Column,
} from "@agento/ui";
import { AdminOnly, CREDENTIAL_ERROR_HINT, message, whole } from "./shared";

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
export const credentialBadge = (c: Pick<Credential, "status" | "enabled">) => !c.enabled
  ? <StatusBadge tone="neutral">disabled</StatusBadge>
  : <StatusBadge tone={inError(c) ? "failed" : "succeeded"}>{c.status}</StatusBadge>;
const freeColor = (pct: number) => (pct < 10 ? "red" : pct < 30 ? "yellow" : "green");
const when = (v: string | null) => (v ? <Timestamp value={v} /> : "—");
const freeOf = (w: LimitWindow) => Math.round(100 - w.used_pct);
/** The smallest free share of the vendor windows; a credential with none sorts first. */
const minFree = (c: Credential) => {
  const w = c.limits?.windows ?? [];
  return w.length ? Math.min(...w.map(freeOf)) : -1;
};

function FreeCell({ c }: { c: Credential }) {
  const windows = c.limits?.windows ?? [];
  if (windows.length) return (
    <Stack gap={2}>
      {windows.map((w) => (
        <Tooltip key={w.label} label={w.resets_at ? <>Resets <Timestamp value={w.resets_at} /></> : "Reset time unknown"}>
          <Group gap="xs" wrap="nowrap">
            <Text size="xs" w={40}>{w.label}</Text>
            <Progress value={freeOf(w)} color={freeColor(freeOf(w))} size="sm" w={80} aria-label={`${c.label} ${w.label} free`} />
            <Text size="xs">{freeOf(w)}%</Text>
          </Group>
        </Tooltip>
      ))}
    </Stack>
  );
  if (c.limits?.balance_usd != null) return `$${c.limits.balance_usd.toFixed(2)}`;
  // `limits_at` with no limits: the check ran and failed (the cron log names the HTTP status).
  if (c.limits == null && c.limits_at) return (
    <Tooltip label={<>Checked <Timestamp value={c.limits_at} />. The vendor refused; see the cron log.</>}>
      <Text size="xs" c="dimmed">Check failed</Text>
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
const LOGIN_ERROR: Record<string, string> = {
  expired: "The login took too long and expired.",
  cli_failed: "The login program stopped with an error.",
  bad_url: "The login program gave a login page address that is not safe to open.",
  disabled: "The credential was disabled during the login. Nothing was saved.",
  busy: "A run was using the credential. Nothing was saved. Try again in a moment.",
  unsupported: "This credential cannot sign in from the panel.",
  abandoned: "The login stopped because its worker stopped.",
};
const loginFailure = (l: Login) => l.status === "cancelled"
  ? "The login was cancelled." : LOGIN_ERROR[l.error_code ?? ""] ?? "The login failed.";
// The server takes 1–300 printable ASCII characters with no space (RSA-OAEP seals at most 318 bytes).
const CODE = /^[\x21-\x7e]{1,300}$/;

function CodeForm({ loginId }: { loginId: number }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const send = useMutation({
    mutationFn: (value: string) => apiFetch(`/api/admin/credential-logins/${loginId}/code`, { method: "POST", json: { code: value } }),
    onError: (e) => setError(message(e)),
  });
  if (send.isSuccess) return <Text>Checking…</Text>;
  const save = () => {
    const value = code.trim();
    if (!CODE.test(value)) { setError("Paste the whole code: 1 to 300 characters, no spaces."); return; }
    setError(null);
    send.mutate(value);
  };
  return (
    <form className="ag-stack" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextField label="Paste the code from the login page" value={code} error={error} autoComplete="off"
        onChange={(e) => setCode(e.currentTarget.value)} />
      <Group><Button type="submit" variant="primary" disabled={send.isPending}>Save</Button></Group>
    </form>
  );
}

function LoginModal({ c, loginId, startError, onRetry, onClose }: {
  c: Credential | null; loginId: number | null; startError: string | null; onRetry: () => void; onClose: () => void;
}) {
  const qc = useQueryClient();
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
        .catch((e) => notifications.show({ color: "red", message: message(e) }));
    }
    onClose();
  };
  const retry = <Group><Button variant="primary" onClick={onRetry}>Try again</Button></Group>;
  let body: ReactNode;
  if (startError) body = <><Text>{startError}</Text>{retry}</>;
  else if (!l || l.status === "pending" || l.status === "starting") body = <Text>Starting…</Text>;
  else if (l.status === "waiting") body = (
    <>
      {l.verify_url && (
        <Anchor href={l.verify_url} target="_blank" rel="noopener noreferrer">Open the login page</Anchor>
      )}
      {l.user_code && (
        <Group gap="xs"><Text>Enter this code on the page:</Text><code>{l.user_code}</code><CopyButton value={l.user_code} /></Group>
      )}
      {l.needs_code && loginId !== null && <CodeForm key={loginId} loginId={loginId} />}
    </>
  );
  else if (l.status === "verifying") body = <Text>Checking…</Text>;
  else if (l.status === "done") body = <Text>Signed in. The credential is saved.</Text>;
  else body = <><Text>{loginFailure(l)}</Text>{retry}</>;
  return (
    <Modal opened={c !== null} onClose={close} title={c ? `Re-login ${c.label}` : ""} centered
      closeButtonProps={{ "aria-label": "Close" }}>
      <div className="ag-stack">
        {poll.error && <Text c="red">{message(poll.error)}</Text>}
        {body}
      </div>
    </Modal>
  );
}

type Action = { kind: "clear-error" | "disable"; credential: Credential };

function UsageDrawer({ c, onClose }: { c: Credential | null; onClose: () => void }) {
  const facts: [string, ReactNode][] = c ? [
    ["Scope", c.scope], ["Label", c.label], ["Status", credentialBadge(c)], ["Last used", when(c.used_at)],
    ["Expires", when(c.expires_at)], ["Token limit (24h)", c.token_limit || "none"], ["Tokens used (24h)", c.tokens_used],
    ["Calls (24h)", c.call_count], ["Local limit (24h)", `${c.pct_free}%`],
    ...(c.limits?.windows ?? []).map((w): [string, ReactNode] => [w.label, w.resets_at
      ? <>{freeOf(w)}% free, resets <Timestamp value={w.resets_at} /></>
      : `${freeOf(w)}% free, reset time unknown`]),
    ...(c.limits?.balance_usd != null ? [["Balance", `$${c.limits.balance_usd.toFixed(2)}`] as [string, ReactNode]] : []),
    ["Checked", when(c.limits_at)],
  ] : [];
  return (
    <Drawer opened={c !== null} onClose={onClose} position="right" title={c ? `Credential ${c.label}` : ""}
      closeButtonProps={{ "aria-label": "Close" }}>
      {c && (
        <>
          <Table>
            <Table.Tbody>
              {facts.map(([k, v]) => <Table.Tr key={k}><Table.Th scope="row">{k}</Table.Th><Table.Td>{v}</Table.Td></Table.Tr>)}
            </Table.Tbody>
          </Table>
          {inError(c) && (
            <Text size="sm" c="dimmed" mt="md">
              {c.error_source === "operator" ? "Set by an operator." : "Set automatically."} {CREDENTIAL_ERROR_HINT}
            </Text>
          )}
        </>
      )}
    </Drawer>
  );
}

export function Credentials() {
  const qc = useQueryClient();
  const creds = useQuery({
    queryKey: ["admin-credentials"],
    queryFn: ({ signal }) => apiFetch<Credential[]>("/api/admin/credentials", { signal }),
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
      notifications.show({ message: a.kind === "disable" ? `Credential ${a.credential.label} disabled.` : `Error cleared on ${a.credential.label}.` });
      setAction(null);
      void qc.invalidateQueries({ queryKey: ["admin-credentials"] });
    },
    onError: (e) => notifications.show({ color: "red", message: message(e) }),
  });

  const columns: Column<Credential>[] = [
    { id: "scope", header: "Scope", cell: (c) => <code>{c.scope}</code>, sortValue: (c) => c.scope },
    { id: "label", header: "Label", cell: (c) => c.label, sortValue: (c) => c.label },
    { id: "status", header: "Status", cell: (c) => whole(credentialBadge(c)), sortValue: (c) => c.status },
    { id: "used", header: "Last used", cell: (c) => when(c.used_at), sortValue: (c) => c.used_at ?? "" },
    { id: "expires", header: "Expires", cell: (c) => when(c.expires_at), sortValue: (c) => c.expires_at ?? "" },
    { id: "used24", header: "Used 24h", cell: (c) => c.tokens_used, sortValue: (c) => c.tokens_used },
    { id: "free", header: "Free", sortValue: minFree, cell: (c) => <FreeCell c={c} /> },
    { id: "actions", header: "Actions", cell: (c) => (
      <Group gap="xs" wrap="nowrap">
        <Button variant="subtle" onClick={() => setDetail(c)}>Usage</Button>
        {c.enabled && c.type === "oauth" && <Button onClick={() => beginLogin(c)}>Re-login</Button>}
        {inError(c) && <Button onClick={() => setAction({ kind: "clear-error", credential: c })}>Clear error</Button>}
        {c.enabled && <Button variant="danger" onClick={() => setAction({ kind: "disable", credential: c })}>Disable</Button>}
      </Group>
    ) },
  ];

  return (
    <AdminOnly>
      <div className="ag-stack">
        <PageHeader title="Credentials" description="Harness credentials, their status, the vendor limits and the last 24 hours of usage." />
        <DataTable caption="Credentials" columns={columns} rows={creds.data} rowKey={(c) => String(c.id)}
          onRowActivate={setDetail} loading={creds.isPending} error={creds.error ? message(creds.error) : null}
          onRetry={() => void creds.refetch()} emptyTitle="No credentials" />
        {creds.data?.some(inError) && <Text size="sm" c="dimmed">{CREDENTIAL_ERROR_HINT}</Text>}
        <ConfirmDialog opened={action !== null} danger={action?.kind === "disable"} busy={run.isPending}
          title={action?.kind === "disable" ? "Disable this credential?" : "Clear the error?"}
          confirmLabel={action?.kind === "disable" ? "Disable" : "Clear error"}
          onCancel={() => setAction(null)} onConfirm={() => action && run.mutate(action)}>
          <p>{action?.kind === "disable"
            ? `${action.credential.label} is no longer picked for a run.`
            : `${action?.credential.label} is picked for runs again.`}</p>
        </ConfirmDialog>
        <UsageDrawer c={detail} onClose={() => setDetail(null)} />
        <LoginModal c={relogin} loginId={start.data?.id ?? null} startError={start.error ? message(start.error) : null}
          onRetry={() => relogin && beginLogin(relogin)}
          onClose={() => { open.current = null; setRelogin(null); start.reset(); }} />
      </div>
    </AdminOnly>
  );
}
