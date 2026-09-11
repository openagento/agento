// Run as `docker compose exec -T toolbox node <this file>`. There is no MCP
// session and no server here, so the config and DB handles the tools normally
// receive from registerTools must be resolved explicitly — otherwise init would
// silently use neither the configured limits/storage_root nor the audit table.
//
// They are INJECTED, not statically imported. `/opt/agento-toolbox-src/` exists
// only inside the container, so a top-level import of it would make this module
// unimportable by the Vitest suite that is supposed to test it.
import { pathToFileURL } from 'node:url';
import { createService } from './service.js';
import { toToolError, ERROR_CODES } from './errors.js';

export async function main(argv, payload, deps) {
  const { loadModuleConfigs, db } = deps;
  const configs = await loadModuleConfigs();
  const config = configs?.versioned_folders;
  if (!config) throw new Error('versioned_folders: module config unavailable (is the module enabled?)');
  // The CLI runs outside the MCP server, so there is no context.log to inherit —
  // it supplies its own function with the SAME (tool, status, details) shape.
  const log = (tool, status, details = '') => console.error(`[${tool}] ${status} ${details}`);
  const service = createService({
    config, db, log, jobId: null, agentViewId: null, actor: argv.actor ?? 'admin',
  });
  // ONE error contract: `main` resolves to `{error_code, message}` on a failed
  // operation and never rejects with a VfError or a host stack trace.
  // `toToolError` is the same mapper the MCP tools use, so an administrator at a
  // terminal and an agent get the same vocabulary. A *configuration* failure
  // above still throws: it means the CLI cannot run at all.
  // The host CLI cannot resolve module config itself — the 3-level fallback reads
  // the DB, which lives on the container network. So it asks the toolbox, where the
  // config already is, rather than duplicating the limits as host-side constants
  // that an operator's `config:set` could never move.
  if (argv.printLimits) {
    return {
      max_file_size: Number(config['limits/max_file_size']),
      max_files: Number(config['limits/max_files']),
      max_total_size: Number(config['limits/max_total_size']),
    };
  }
  try {
    return await service.init(payload.folder_code, { files: payload.files });
  } catch (err) {
    return toToolError(err, log);
  }
}

function parseArgv(args) {
  const parsed = {};
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === '--actor') { parsed.actor = args[i + 1]; i += 1; }
    if (args[i] === '--print-limits') { parsed.printLimits = true; }
  }
  return parsed;
}

// Direct execution only: this branch never runs under Vitest, so the
// container-only modules are loaded lazily, here.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  // ONE outer catch, and it is the whole "never a traceback" contract. Everything
  // in this block can throw for a reason `main` does not model — a dynamic import
  // that cannot resolve, malformed stdin JSON, an unreachable DB, a config the
  // module has not been enabled for — and Node's default handler prints the entire
  // ESM stack with absolute container paths. The host side then has to sanitize
  // that stderr by guessing at stack shapes, which is a game it loses one shape at
  // a time. So the child never writes a stack: it writes the same
  // `{error_code, message}` line as the successful path, on stdout, and exits 1.
  try {
    const [{ loadModuleConfigs }, db] = await Promise.all([
      import('/opt/agento-toolbox-src/config-loader.js'),
      import('/opt/agento-toolbox-src/db.js'),
    ]);
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    const payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    const result = await main(parseArgv(process.argv.slice(2)), payload, { loadModuleConfigs, db });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    // The shell needs a status code, and `main` deliberately does not throw for it.
    process.exit(result?.error_code ? 1 : 0);
  } catch {
    process.stdout.write(`${JSON.stringify({
      error_code: ERROR_CODES.GIT_OPERATION_FAILED, message: 'the toolbox command failed',
    })}\n`);
    process.exit(1);
  }
}
