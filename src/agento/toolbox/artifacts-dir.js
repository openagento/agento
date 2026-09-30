export const FALLBACK_ARTIFACTS_DIR = '/workspace/artifacts/_fallback';

// A run id names the LAST segment of the dir, so it must be one segment and nothing
// else: no dot (so no `..`), no separator. `_fallback` passes that class but means
// "unknown session", which is the one thing a run id must never be.
const RUN_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

/** The per-run artifacts dir this MCP session serves.
 *
 * The last segment identifies the RUN and has to be unique per run: a job gives its
 * numeric id, an interactive `agento run` its string run id — the same segment its
 * artifacts dir already ends with. A session that names neither gets `_fallback`, which
 * EVERY such session shares; the desk tools refuse that path rather than let two runs
 * write the same files. Naming nothing is the honest answer, not a shared desk. */
export function buildArtifactsDir(agentViewMeta, jobId, runId) {
  if (!agentViewMeta) return FALLBACK_ARTIFACTS_DIR;
  const safeWs = String(agentViewMeta.workspaceCode || '').replace(/[^a-zA-Z0-9_-]/g, '');
  const safeAv = String(agentViewMeta.agentViewCode || '').replace(/[^a-zA-Z0-9_-]/g, '');
  let session = '';
  if (jobId) {
    session = String(jobId).replace(/[^0-9]/g, '');
  } else if (typeof runId === 'string' && runId !== '_fallback' && RUN_ID_RE.test(runId)) {
    session = runId;
  }
  if (safeWs && safeAv && session) {
    return `/workspace/artifacts/${safeWs}/${safeAv}/${session}`;
  }
  return FALLBACK_ARTIFACTS_DIR;
}
