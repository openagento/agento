import { apiFetch, ApiError, useQuery } from "@agento/api";

export interface Artifact {
  artifact_code: string; title: string | null; current_version: string | null; auth_enabled: boolean; open_drafts: unknown[];
}

export const BASE = "/api/versioned_artifacts/artifacts";
export const message = (e: unknown) => (e instanceof ApiError ? e.message : "The request failed.");

/** Every artifact of the store (global: the leaf `versioned_artifacts` has no scope). */
export const useArtifacts = () => useQuery({
  queryKey: ["artifacts"],
  queryFn: ({ signal }) => apiFetch<Artifact[]>(BASE, { signal }),
});
