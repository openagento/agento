import { useState, type ReactNode } from "react";
import { Drawer, Group, Progress, Table, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { apiFetch, useMutation, useQuery, useQueryClient } from "@agento/api";
import { Button, ConfirmDialog, DataTable, PageHeader, StatusBadge, Timestamp, type Column } from "@agento/ui";
import { AdminOnly, CREDENTIAL_ERROR_HINT, message, whole } from "./shared";

// An allow-list on the server: no token and no error_msg ever arrives here (SEC-6).
export interface Credential {
  id: number; scope: string; label: string; status: string; enabled: boolean;
  error_source: "auto" | "operator" | null; used_at: string | null; expires_at: string | null;
  token_limit: number; tokens_used: number; call_count: number; pct_free: number;
}

/** The error state is `status`, as in the TUI; a stale message on an ok credential is not an error. */
export const inError = (c: Pick<Credential, "status">) => c.status === "error";
export const credentialBadge = (c: Pick<Credential, "status" | "enabled">) => !c.enabled
  ? <StatusBadge tone="neutral">disabled</StatusBadge>
  : <StatusBadge tone={inError(c) ? "failed" : "succeeded"}>{c.status}</StatusBadge>;
const freeColor = (pct: number) => (pct < 10 ? "red" : pct < 30 ? "yellow" : "green");
const when = (v: string | null) => (v ? <Timestamp value={v} /> : "—");

type Action = { kind: "clear-error" | "disable"; credential: Credential };

function UsageDrawer({ c, onClose }: { c: Credential | null; onClose: () => void }) {
  const facts: [string, ReactNode][] = c ? [
    ["Scope", c.scope], ["Label", c.label], ["Status", credentialBadge(c)], ["Last used", when(c.used_at)],
    ["Expires", when(c.expires_at)], ["Token limit (24h)", c.token_limit || "none"], ["Tokens used (24h)", c.tokens_used],
    ["Calls (24h)", c.call_count], ["Free", `${c.pct_free}%`],
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
    { id: "free", header: "Free %", sortValue: (c) => c.pct_free, cell: (c) => (
      <Group gap="xs" wrap="nowrap">
        <Progress value={c.pct_free} color={freeColor(c.pct_free)} w={80} aria-label={`${c.label} free`} />
        <Text size="xs">{c.pct_free}%</Text>
      </Group>
    ) },
    { id: "actions", header: "Actions", cell: (c) => (
      <Group gap="xs" wrap="nowrap">
        <Button variant="subtle" onClick={() => setDetail(c)}>Usage</Button>
        {inError(c) && <Button onClick={() => setAction({ kind: "clear-error", credential: c })}>Clear error</Button>}
        {c.enabled && <Button variant="danger" onClick={() => setAction({ kind: "disable", credential: c })}>Disable</Button>}
      </Group>
    ) },
  ];

  return (
    <AdminOnly>
      <div className="ag-stack">
        <PageHeader title="Credentials" description="Harness credentials, their status and the last 24 hours of usage." />
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
      </div>
    </AdminOnly>
  );
}
