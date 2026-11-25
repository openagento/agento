// The Tools and Skills bodies: one checkbox tree per scope, edited as a draft and saved with one
// batch of `PUT /api/admin/config` (the Roles ACL pattern). Drafts are held PER SCOPE, because the
// scope lives in the URL and this component stays mounted across a change of it: an unbound draft
// would be written to whatever scope is on screen when Save is pressed (useConfigWrite resolves the
// scope at render). A draft is hidden while another scope is on screen and comes back when you
// return; it dies with the page, like any other unsaved form.
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { Badge, Group, Tooltip } from "@mantine/core";
import { apiFetch, useErrorText, useQuery, useQueryClient, useSession } from "@agento/api";
import { EmptyState, ErrorState, LabelBadge, LoadingState, useT, type Translate } from "@agento/ui";
import type { TreeNodeData } from "@mantine/core";
import { ResourceTree, UnsavedBar } from "../ResourceTree";
import { changeCount } from "../roleTree";
import { initialChecked, lockedLeaves, toTreeData, toWrites, type EnablementInfo, type Groups } from "../enablementTree";
import { trifecta } from "../trifecta";
import { useConfigWrite, useScope } from "./shared";
import { useToolLabels } from "./ToolLabels";
import { TrifectaAlert } from "./TrifectaAlert";
import { TIGHT } from "../layout";

/** `names`: label key → name, for the admin role only; without it no label shows. */
function info(t: Translate, node: TreeNodeData, names: Map<string, string> | null): ReactNode {
  const { blockedBy, inherited, labels } = (node.nodeProps ?? {}) as EnablementInfo;
  const state = blockedBy
    ? <Tooltip label={t("enablement.blocked_by", { name: blockedBy })} withArrow><Badge variant="light" color="gray" size="sm" tt="none">{t("enablement.blocked")}</Badge></Tooltip>
    : inherited ? <Badge variant="light" color="gray" size="sm" tt="none">{t("enablement.inherited")}</Badge> : null;
  if (!names || !labels?.length) return state;
  // One wrapping group: when it does not fit beside the name, the tree row puts it on its own line.
  return (
    <Group gap={TIGHT} wrap="wrap" flex="1 1 auto">
      {state}
      {labels.map((k) => <LabelBadge key={k} labelKey={k} name={names.get(k) ?? k} />)}
    </Group>
  );
}

/** `listKey` is this screen's own query key: Tools and Skills invalidate different lists.
 *  `toolRisk`: the items are tools, so an admin sees their labels and the lethal-trifecta warning. */
export function EnablementTree<T>({ listKey, url, groupsOf, emptyTitle, emptyText, toolRisk = false }: {
  listKey: string; url: string; groupsOf: (data: T) => Groups; emptyTitle: string; emptyText: ReactNode; toolRisk?: boolean;
}) {
  const admin = useSession()?.role === "admin";
  const risk = toolRisk && admin;
  const labels = useToolLabels({ enabled: risk });
  const names = useMemo(() => (risk ? new Map((labels.data ?? []).map((l) => [l.key, l.name])) : null), [risk, labels.data]);
  const { ready, query } = useScope();
  const qc = useQueryClient();
  const t = useT("panel");
  const errorText = useErrorText("panel");
  const renderInfo = useCallback((node: TreeNodeData) => info(t, node, names), [t, names]);
  const list = useQuery({
    queryKey: [listKey, query], enabled: ready,
    queryFn: ({ signal }) => apiFetch<T>(`${url}?${query}`, { signal }),
  });
  const write = useConfigWrite(listKey);
  const [edits, setEdits] = useState<Record<string, Set<string>>>({});

  const groups = useMemo(() => (list.data ? groupsOf(list.data) : []), [list.data, groupsOf]);
  const toData = useCallback((search: string) => toTreeData(groups, search), [groups]);
  const server = useMemo(() => initialChecked(groups), [groups]);
  const locked = useMemo(() => lockedLeaves(groups), [groups]);
  const checked = edits[query] ?? server;
  const changes = changeCount(checked, server);
  const items = useMemo(() => groups.flatMap((g) => g.items), [groups]);
  const warning = useMemo(() => (risk ? trifecta(items, checked) : null), [risk, items, checked]);

  const save = () => {
    const at = query;
    const sent = edits[at];
    if (!sent) return;
    write.mutate(toWrites(sent, server), {
      // onSuccess, never onSettled: a failed batch rejects, and the draft is the only retry path.
      // refetchType "all": the saved scope may no longer be on screen, and the default ("active")
      // would resolve without fetching it, so the draft would clear over a stale cache entry.
      // throwOnError: invalidateQueries swallows a failed refetch, which would clear the draft over
      // the same stale entry — the draft is what the user still has, so it outlives any doubt.
      onSuccess: async () => {
        try {
          await qc.invalidateQueries({ queryKey: [listKey, at], refetchType: "all" }, { throwOnError: true });
        } catch {
          return;
        }
        setEdits((e) => (e[at] === sent ? omit(e, at) : e));
      },
    });
  };

  if (!ready) return <EmptyState title={t("enablement.choose_scope")} />;
  if (list.isPending) return <LoadingState />;
  if (list.error) return <ErrorState message={errorText(list.error, "enablement.load_failed")} onRetry={() => void list.refetch()} />;
  if (!groups.length) return <EmptyState title={emptyTitle}>{emptyText}</EmptyState>;
  return (
    <>
      {warning?.complete && <TrifectaAlert categories={warning.categories} />}
      <ResourceTree key={query} toData={toData} checked={checked} locked={locked} busy={write.isPending}
        onChange={(set) => setEdits((e) => ({ ...e, [query]: set }))} renderInfo={renderInfo}
        emptyTitle={emptyTitle} emptyText={emptyText} />
      {changes > 0 && (
        <UnsavedBar changes={changes} busy={write.isPending} onSave={save}
          onReset={() => setEdits((e) => omit(e, query))} />
      )}
    </>
  );
}

const omit = (map: Record<string, Set<string>>, key: string) => {
  const next = { ...map };
  delete next[key];
  return next;
};
