// POST /internal/tools/{name}:invoke — the canonical internal tool endpoint. Bearer only: the
// guard refuses a `?cap=`. The body IS the tool's arguments object. Express reads `:` as a
// parameter marker, hence the RegExp. The body parser runs AFTER the guard, so an
// unauthenticated body is never parsed, and on this route only: SSE needs the raw stream.
// ponytail: per-invoke registration, cache per (agent_view, job) with an idle TTL if invoke latency matters.
import express from 'express';
import { executeTool, HTTP_STATUS } from './dispatcher.js';
import { errorCategory } from './log.js';

export const INVOKE_ROUTE = /^\/internal\/tools\/([a-z0-9_]+):invoke$/;

export function installInvokeRoute(app, { guard, deps, loadRegistryFor, log }) {
  // An unreadable body never reaches the dispatcher: there is no execution to audit yet. Every
  // parser error ends here — Express's default handler would answer with the stack.
  function bodyError(err, req, res, _next) {
    log('invoke', 'ERROR', `unreadable request body: ${err?.type || errorCategory(err)}`);
    return res.status(400).json({ ok: false, error: { code: 'invalid_arguments', message: 'unreadable JSON body' } });
  }

  // express.json() skips a body it does not recognise and leaves `{}`, which would run a
  // zero-argument tool on arbitrary bytes. `req.is` is null for a request with no body at all.
  function requireJson(req, res, next) {
    if (req.is('application/json')) return next();
    log('invoke', 'ERROR', 'request body is not application/json');
    return res.status(400).json({ ok: false, error: { code: 'invalid_arguments', message: 'body must be application/json' } });
  }

  async function invoke(req, res) {
    const authContext = req.capability;
    const response = await executeTool(authContext, req.params[0], req.body, {
      ...deps, endpoint: 'invoke', loadRegistry: loadRegistryFor(authContext),
    });
    if (response.ok) return res.status(200).json(response);
    // An isError body can quote an upstream response or an exception; an invoke caller (a browser
    // or a miniapp) gets only the code. MCP keeps the body: the agent needs it to recover.
    const { ok, error, execution_id } = response;
    return res.status(HTTP_STATUS[error.code]).json({ ok, error, execution_id });
  }

  // An empty body is not JSON, however it arrives (`Content-Length: 00`, empty chunked, a gzip of
  // nothing): the check runs on the DECODED bytes, and bodyError answers the throw.
  const json = express.json({ strict: false, verify: (_req, _res, buf) => {
    if (buf.length === 0) throw new Error('empty body');
  } });
  app.post(INVOKE_ROUTE, guard, requireJson, json, bodyError, invoke);
}
