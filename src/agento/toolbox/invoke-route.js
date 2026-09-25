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

  async function invoke(req, res) {
    const authContext = req.capability;
    const response = await executeTool(authContext, req.params[0], req.body, {
      ...deps, endpoint: 'invoke', loadRegistry: loadRegistryFor(authContext),
    });
    if (response.ok) return res.status(200).json(response);
    // An isError body can quote an upstream response or an exception; an invoke caller (a browser
    // or a miniapp) gets only the code. MCP keeps the body: the agent needs it to recover.
    const { result: _dropped, ...refusal } = response;
    return res.status(HTTP_STATUS[response.error.code]).json(refusal);
  }

  app.post(INVOKE_ROUTE, guard, express.json({ strict: false }), bodyError, invoke);
}
