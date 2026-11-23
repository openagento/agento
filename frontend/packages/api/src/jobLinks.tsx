// The links from a job's detail to the pages modules have about it (`PanelModule.jobLink`). The
// panel App passes its registry here: a module's panel cannot import the panel app.
import { createContext, useContext, type ReactNode } from "react";
import { useQueries } from "@tanstack/react-query";
import { apiFetch } from "./apiFetch";
import type { PanelModule } from "./contract";
import { LIVE_REFETCH_MS } from "./query";

const Modules = createContext<PanelModule[]>([]);

export function PanelModulesProvider({ modules, children }: { modules: PanelModule[]; children: ReactNode }) {
  return <Modules.Provider value={modules}>{children}</Modules.Provider>;
}

export interface JobLinkTarget { label: string; to: string }

/** Asked again every LIVE_REFETCH_MS while the detail is open: a conversation made by the job's first
 *  run appears, and a lost right or a disabled module (404) removes the link. */
export function useJobLinks(jobId: number | null): JobLinkTarget[] {
  const linked = useContext(Modules).filter((m) => m.jobLink);
  const answers = useQueries({
    queries: linked.map((m) => ({
      queryKey: ["job-link", m.id, jobId],
      enabled: jobId !== null,
      retry: false,
      refetchInterval: LIVE_REFETCH_MS,
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        apiFetch<{ id?: unknown }>(m.jobLink!.api.replace("{job}", String(jobId)), { signal }),
    })),
  });
  // A 404 after a 200 is status "error" while TanStack still holds the old data: only success shows.
  // contract.ts checked `to` on the promise that only digits replace `{id}`: an answer without a
  // positive integer id shows no link.
  return linked.flatMap((m, i) => {
    const answer = answers[i];
    const id = answer.status === "success" ? answer.data?.id : undefined;
    return typeof id === "number" && Number.isSafeInteger(id) && id > 0
      ? [{ label: m.jobLink!.label, to: m.jobLink!.to.replace("{id}", String(id)) }]
      : [];
  });
}
