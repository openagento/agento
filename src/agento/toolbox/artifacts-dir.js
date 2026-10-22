export const FALLBACK_ARTIFACTS_DIR = '/workspace/artifacts/_fallback';

// A run id names the LAST segment of the dir, so it must be one segment and nothing
// else: no dot (so no `..`), no separator. `_fallback` passes that class but means
// "unknown session", which is the one thing a run id must never be.
const RUN_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

// A workspace or view code names a dir next to `_threads` and `_fallback`, so it may not
// start with `_` (framework/artifacts_dir.py `build_artifacts_dir` refuses the same codes).
const RUN_DIR_CODE_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

/** The per-run artifacts dir this MCP session serves.
 *
 * The last segment identifies the RUN and has to be unique per run: a job gives its
 * numeric id, an interactive `agento run` its string run id — the same segment its
 * artifacts dir already ends with. A session that names neither gets `_fallback`, which
 * EVERY such session shares; the desk tools refuse that path rather than let two runs
 * write the same files. Naming nothing is the honest answer, not a shared desk. */
export function buildArtifactsDir(agentViewMeta, jobId, runId) {
  if (!agentViewMeta) return FALLBACK_ARTIFACTS_DIR;
  const ws = String(agentViewMeta.workspaceCode || '');
  const av = String(agentViewMeta.agentViewCode || '');
  let session = '';
  if (jobId) {
    session = String(jobId).replace(/[^0-9]/g, '');
  } else if (typeof runId === 'string' && runId !== '_fallback' && RUN_ID_RE.test(runId)) {
    session = runId;
  }
  if (RUN_DIR_CODE_RE.test(ws) && RUN_DIR_CODE_RE.test(av) && session) {
    return `/workspace/artifacts/${ws}/${av}/${session}`;
  }
  return FALLBACK_ARTIFACTS_DIR;
}

// A thread's random key (framework/artifacts_dir.py `thread_dir`): 32 lowercase hex.
const THREAD_KEY_RE = /^[0-9a-f]{32}$/;

/** `downloads/` of the thread whose key is `key`: the tool-owned dir cron made for it
 * (framework/artifacts_dir.py `prepare_thread_dir`). Null for anything that is not a key. */
export function buildDownloadsDir(key) {
  return typeof key === 'string' && THREAD_KEY_RE.test(key)
    ? `/workspace/artifacts/_threads/${key}/downloads`
    : null;
}

/** The downloads dir of the job's thread, or null (no job, or a job with no thread). The
 * job id comes from the capability row (SEC-7), never from a tool argument. */
export async function loadDownloadsDir(query, jobId) {
  if (!jobId) return null;
  const [rows] = await query(
    'SELECT t.`key` FROM job j JOIN thread t ON t.id = j.thread_id WHERE j.id = ?', [jobId],
  );
  return buildDownloadsDir(rows[0]?.key);
}
