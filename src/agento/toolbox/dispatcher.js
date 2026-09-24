// One dispatcher for every tool call, on both transports: MCP (`/mcp`, `/messages`) and
// `POST /internal/tools/{name}:invoke`. Authorization is evaluated per CALL, not per session,
// which is what makes revocation and tool-disablement take effect on the next call.
import { createHash, randomUUID } from 'node:crypto';
import { CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { z } from 'zod';
import { ENDPOINT_TRANSPORT, SINGLE_USE_KINDS } from './auth-context.js';
import { ScopeResolutionError, ScopeUnavailableError } from './config-loader.js';
import { errorCategory } from './log.js';

export const HTTP_STATUS = Object.freeze({
  unauthorized: 403,
  not_found: 404,
  invalid_arguments: 400,
  tool_error: 422,
  unavailable: 503,
});

// Single use, atomically: of two concurrent presentations exactly one sees affectedRows 1.
export const CONSUME_CAPABILITY_SQL =
  'UPDATE toolbox_capability SET consumed_at = NOW() ' +
  'WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > NOW()';

const INSERT_AUDIT_SQL =
  'INSERT INTO tool_invocation (execution_id, capability_id, transport, actor, subject_id, on_behalf_of, ' +
  'tool_name, args_sha256, agent_view_id, workspace_id, app_artifact_code, app_version_id, app_launch_id, outcome) ' +
  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')";
const FINALIZE_AUDIT_SQL = 'UPDATE tool_invocation SET outcome = ? WHERE execution_id = ?';

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]));
  }
  return value;
}

// The digest of the arguments, never their values: arguments carry message bodies and ids.
export function argsSha256(args) {
  return createHash('sha256').update(JSON.stringify(canonical(args ?? null)) ?? 'null').digest('hex');
}

export function createAuditStore(query) {
  return {
    async insert(row) {
      await query(INSERT_AUDIT_SQL, [
        row.execution_id, row.capability_id, row.transport, row.actor, row.subject_id, row.on_behalf_of,
        row.tool_name, row.args_sha256, row.agent_view_id, row.workspace_id,
        row.app?.artifact_code ?? null, row.app?.version_id ?? null, row.app?.launch_id ?? null,
      ]);
    },
    async finalize(executionId, outcome) {
      await query(FINALIZE_AUDIT_SQL, [outcome, executionId]);
    },
  };
}

export function createConsumer(query) {
  return async (capabilityId) => {
    const [result] = await query(CONSUME_CAPABILITY_SQL, [capabilityId]);
    return result?.affectedRows === 1;
  };
}

function argumentSchema(schema) {
  if (schema instanceof z.ZodObject) return schema.strict();
  if (schema instanceof z.ZodType) return schema;
  return z.object(schema ?? {}).strict();
}

function failure(code, message) {
  return { ok: false, error: { code, message } };
}

function isScopeFailure(err) {
  return err instanceof ScopeUnavailableError || err instanceof ScopeResolutionError;
}

async function authorizeAndRun(authContext, toolName, args, deps) {
  const { endpoint, consume, reverify, loadRegistry, loadOverrides, extra, log = () => {} } = deps;

  if (SINGLE_USE_KINDS.includes(authContext.kind) && !(await consume(authContext.capability_id))) {
    return failure('unauthorized', 'capability already used, revoked or expired');
  }

  let registry;
  try {
    registry = await loadRegistry();
  } catch {
    return failure('unavailable', 'tool registry unavailable');
  }

  // Both the capability and its source are checked on EVERY call — a session opened with a
  // live token does not keep it live.
  const derived = await reverify(authContext.capability_id, { endpoint });
  if (!derived) return failure('unauthorized', 'capability no longer valid');
  const context = derived.context;

  // Checked before the registry: a module that registered some tools and then threw is
  // half-initialized, and none of its tools may run.
  if (registry.unavailableTools?.has(toolName)) return failure('unavailable', `tool ${toolName} is unavailable`);
  const entry = registry.tools.get(toolName);
  if (!entry) return failure('not_found', `unknown tool ${toolName}`);

  const overrides = await loadOverrides(context);
  // A disabled tool is indistinguishable from an absent one to the caller.
  if (!registry.isEnabled(toolName, overrides)) return failure('not_found', `unknown tool ${toolName}`);

  if (context.tool_ceiling !== null && !context.tool_ceiling.includes(toolName)) {
    return failure('unauthorized', `tool ${toolName} is outside the launch's tool ceiling`);
  }
  if (derived.permitted_tools !== null && !derived.permitted_tools.includes(toolName)) {
    return failure('unauthorized', `tool ${toolName} is not permitted for this user`);
  }

  const parsed = argumentSchema(entry.schema).safeParse(args ?? {});
  if (!parsed.success) {
    const where = parsed.error.issues.map(i => i.path.join('.') || '(root)').join(', ');
    return failure('invalid_arguments', `invalid arguments: ${where}`);
  }

  let result;
  try {
    result = await entry.handler(parsed.data, extra);
  } catch (err) {
    if (isScopeFailure(err)) return failure('unavailable', 'scope resolution unavailable');
    // A thrown message can quote an upstream response body; the caller gets a fixed text.
    log('dispatch', 'ERROR', `tool ${toolName} threw: ${errorCategory(err)}`);
    return failure('tool_error', 'tool failed');
  }
  if (result?.isError) return { ...failure('tool_error', 'tool reported an error'), result };
  return { ok: true, result };
}

/**
 * executeTool(authContext, toolName, arguments) -> ToolInvocationResult
 *
 * The audit row is written BEFORE anything runs and finalized on every exit, so no execution
 * — and no refusal — is unaudited. A failed audit insert answers `unavailable` and runs nothing.
 */
export async function executeTool(authContext, toolName, args, deps) {
  const { audit, endpoint, log = () => {} } = deps;
  const executionId = randomUUID();
  try {
    await audit.insert({
      execution_id: executionId,
      capability_id: authContext.capability_id,
      transport: ENDPOINT_TRANSPORT[endpoint],
      actor: authContext.actor,
      subject_id: authContext.subject_id,
      on_behalf_of: authContext.on_behalf_of,
      tool_name: toolName,
      args_sha256: argsSha256(args),
      agent_view_id: authContext.agent_view_id,
      workspace_id: authContext.workspace_id,
      app: authContext.app,
    });
  } catch (err) {
    log('audit', 'ERROR', `audit insert failed: ${errorCategory(err)}`);
    return { ...failure('unavailable', 'audit unavailable'), execution_id: executionId };
  }

  let response;
  try {
    response = await authorizeAndRun(authContext, toolName, args, deps);
  } catch (err) {
    log('dispatch', 'ERROR', `tool ${toolName} dispatch failed: ${errorCategory(err)}`);
    response = failure('unavailable', 'service unavailable');
  }
  try {
    await audit.finalize(executionId, response.ok ? 'ok' : response.error.code);
  } catch (err) {
    // The row stays `pending`, which is itself the signal.
    log('audit', 'ERROR', `audit finalize failed: ${errorCategory(err)}`);
  }
  return { ...response, execution_id: executionId };
}

// MCP answers every outcome as a CallToolResult; the SDK validates the shape.
export function toCallToolResult(response) {
  if (response.ok) return response.result;
  if (response.result) return response.result;
  return {
    content: [{ type: 'text', text: `${response.error.code}: ${response.error.message}` }],
    isError: true,
  };
}

// The SDK validates arguments BEFORE a per-tool handler runs, so an invalid call would never
// reach the dispatcher and go unaudited. tools/call is therefore answered by executeTool; the
// SDK keeps tools/list. With no tool registered the SDK declares no tools capability, and
// there is nothing to call.
export function installToolDispatch(server, registration, authContext, deps) {
  if (registration.toolNames.length === 0) return;
  server.server.setRequestHandler(CallToolRequestSchema, async (req, extra) => toCallToolResult(
    await executeTool(authContext, req.params.name, req.params.arguments ?? {}, {
      ...deps, extra, loadRegistry: async () => registration,
    }),
  ));
}
