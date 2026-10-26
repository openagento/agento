import { createService } from './service.js';
import { ArtifactError, ERROR_CODES, toToolError } from './errors.js';

// Store administration for the trusted backend (web, cron), under /admin. server.js guards
// /admin with the `admin` endpoint (only a viewless internal_rest reaches it, and only the
// backend can mint one) and parses JSON bodies there. The actor is the capability's subject,
// never a body field.

const STATUS = {
  [ERROR_CODES.ARTIFACT_NOT_FOUND]: 404,
  [ERROR_CODES.VERSION_NOT_FOUND]: 404,
  [ERROR_CODES.ARTIFACT_ACCESS_DENIED]: 403,
  [ERROR_CODES.CURRENT_VERSION_CHANGED]: 409,
};
const SERVER_ERRORS = new Set([ERROR_CODES.STORAGE_OPERATION_FAILED, ERROR_CODES.PUBLISH_FAILED]);

function sendError(res, err, log) {
  const body = toToolError(err, log);
  const status = STATUS[body.error_code]
    ?? (err instanceof ArtifactError && !SERVER_ERRORS.has(body.error_code) ? 400 : 500);
  if (err?.current_version) body.current_version = err.current_version;
  return res.status(status).json(body);
}

/** One admin route: a service with no scope (`admin: true`), built per request for the
 *  capability's subject. `fn(service, req)` returns the JSON body. */
export function adminRoute({ moduleConfigs, db, log, retentionPins = [] }, fn) {
  return async (req, res) => {
    const cap = req.capability;
    // The guard admits only a viewless capability; this holds if a mount ever drifts.
    if (!cap || cap.agent_view_id !== null || typeof cap.subject_id !== 'string') {
      return res.status(403).json({ error: 'forbidden' });
    }
    try {
      const service = createService({ config: moduleConfigs?.versioned_artifacts ?? {}, db, log,
        admin: true, agentViewId: null, actor: cap.subject_id, retentionPins });
      return res.json(await fn(service, req));
    } catch (err) {
      return sendError(res, err, log);
    }
  };
}

export function register(_server, context) {
  const { app } = context;
  if (!app) return;   // MCP sessions: nothing here is a tool
  const route = (fn) => adminRoute(context, fn);
  const base = '/admin/versioned-artifacts';

  app.get(base, route(async (svc) => ({ artifacts: await svc.listArtifacts() })));
  app.get(`${base}/:code/versions`, route(async (svc, req) => ({ versions: await svc.listVersions(req.params.code) })));
  // Make current (rollback): the CAS of `publish`; a 409 carries the real current.
  app.post(`${base}/:code/publish`, route(async (svc, req) => {
    const { version_id: versionId, expected_current_version: expected } = req.body ?? {};
    try {
      return await svc.publish(req.params.code, versionId, expected);
    } catch (err) {
      if (err?.code === ERROR_CODES.CURRENT_VERSION_CHANGED) {
        err.current_version = (await svc.getCurrent(req.params.code)).current_version;
      }
      throw err;
    }
  }));
  app.delete(`${base}/:code`, route(async (svc, req) => svc.remove(req.params.code)));
  app.post(`${base}/:code/auth`, route(async (svc, req) => {
    const { user = null, password = null, disable = false } = req.body ?? {};
    return svc.setAuth(req.params.code, { user, password, disable: disable === true });
  }));
}
