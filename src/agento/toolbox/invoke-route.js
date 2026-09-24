// POST /internal/tools/{name}:invoke — the canonical internal tool endpoint. Bearer only: the
// guard refuses a `?cap=`. The body IS the tool's arguments object. Express reads `:` as a
// parameter marker, hence the RegExp. The body parser runs AFTER the guard, so an
// unauthenticated body is never parsed, and on this route only: SSE needs the raw stream.
// ponytail: per-invoke registration, cache per (agent_view, job) with an idle TTL if invoke latency matters.
import express from 'express';
import { executeTool, HTTP_STATUS } from './dispatcher.js';

export const INVOKE_ROUTE = /^\/internal\/tools\/([a-z0-9_]+):invoke$/;

export function installInvokeRoute(app, { guard, deps, loadRegistryFor, log }) {
  // Malformed JSON never reaches the dispatcher: there is no execution to audit yet.
  function bodyError(err, req, res, next) {
    if (err?.type === 'entity.parse.failed' || err?.type === 'entity.too.large') {
      log('invoke', 'ERROR', `unreadable request body: ${err.type}`);
      return res.status(400).json({ ok: false, error: { code: 'invalid_arguments', message: 'unreadable JSON body' } });
    }
    return next(err);
  }

  async function invoke(req, res) {
    const authContext = req.capability;
    const response = await executeTool(authContext, req.params[0], req.body, {
      ...deps, endpoint: 'invoke', loadRegistry: loadRegistryFor(authContext),
    });
    return res.status(response.ok ? 200 : HTTP_STATUS[response.error.code]).json(response);
  }

  app.post(INVOKE_ROUTE, guard, express.json({ strict: false }), bodyError, invoke);
}
