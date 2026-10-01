// Runtime module filter (PRD E8 §7), failing closed: an entry shows only after its
// module's own probe answered 2xx. A disabled module's routes answer 404.
import { apiFetch, useQuery, type PanelModule } from "@agento/api";

export function useAvailable(m: PanelModule): "pending" | "on" | "off" {
  const q = useQuery({
    queryKey: ["availability", m.id],
    queryFn: ({ signal }) => apiFetch(m.availability.probe, { signal }),
    retry: false,
    staleTime: 60_000,
  });
  return q.isPending ? "pending" : q.isSuccess ? "on" : "off";
}
