// The Tools and Skills bodies: one checkbox tree per scope, edited as a draft and saved with one
// batch of `PUT /api/admin/config` (the Roles ACL pattern). Drafts are held PER SCOPE, because the
// scope lives in the URL and this component stays mounted across a change of it: an unbound draft
// would be written to whatever scope is on screen when Save is pressed (useConfigWrite resolves the
// scope at render). A draft is hidden while another scope is on screen and comes back when you
// return; it dies with the page, like any other unsaved form.
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { Badge, Tooltip } from "@mantine/core";
import { apiFetch, useQuery, useQueryClient } from "@agento/api";
import { EmptyState, ErrorState, LoadingState } from "@agento/ui";
import type { TreeNodeData } from "@mantine/core";
import { ResourceTree, UnsavedBar } from "../ResourceTree";
import { changeCount } from "../roleTree";
import { initialChecked, lockedLeaves, toTreeData, toWrites, type EnablementInfo, type Groups } from "../enablementTree";
import { message, useConfigWrite, useScope } from "./shared";

function info(node: TreeNodeData): ReactNode {
  const { blockedBy, inherited } = (node.nodeProps ?? {}) as EnablementInfo;
  if (blockedBy) return <Tooltip label={`Blocked by ${blockedBy}`} withArrow><Badge variant="light" color="gray" size="sm">blocked</Badge></Tooltip>;
  if (inherited) return <Badge variant="light" color="gray" size="sm">inherited</Badge>;
  return null;
}

/** `listKey` is this screen's own query key: Tools and Skills invalidate different lists. */
export function EnablementTree<T>({ listKey, url, groupsOf, emptyTitle, emptyText }: {
  listKey: string; url: string; groupsOf: (data: T) => Groups; emptyTitle: string; emptyText: ReactNode;
}) {
  const { ready, query } = useScope();
  const qc = useQueryClient();
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

  if (!ready) return <EmptyState title="Choose a scope" />;
  if (list.isPending) return <LoadingState />;
  if (list.error) return <ErrorState message={message(list.error)} onRetry={() => void list.refetch()} />;
  if (!groups.length) return <EmptyState title={emptyTitle}>{emptyText}</EmptyState>;
  return (
    <>
      <ResourceTree key={query} toData={toData} checked={checked} locked={locked} busy={write.isPending}
        onChange={(set) => setEdits((e) => ({ ...e, [query]: set }))} renderInfo={info}
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
