// Run as `docker compose exec -T toolbox node <this file> --op <op>`: the operator side of
// activation (PRD E6 §4). There is no tool for it. Same contract as the
// versioned_artifacts CLI: one `{error_code, message}` line on failure, never a stack.
import { pathToFileURL } from 'node:url';
import { createMiniapps, toMiniappError } from './miniapps.js';

export async function main(argv, payload, deps) {
  const { loadModuleConfigs, db } = deps;
  const configs = await loadModuleConfigs();
  const vaConfig = configs?.versioned_artifacts;
  if (!vaConfig) throw new Error('miniapps: versioned_artifacts config unavailable (is the module enabled?)');
  const log = (tool, status, details = '') => console.error(`[${tool}] ${status} ${details}`);
  // `admin: true` here and nowhere else, as in the versioned_artifacts CLI.
  const apps = createMiniapps({ vaConfig, db, log, actor: argv.actor ?? 'admin', admin: true });
  try {
    switch (argv.op) {
      case 'activate':
        return await apps.activate(payload.artifact_code, payload.version_id, payload.actions ?? null);
      case 'deactivate':
        return await apps.deactivate(payload.artifact_code, payload.version_id);
      case 'list':
        return { activations: await apps.listActivations() };
      default:
        return { error_code: 'INVALID_OPERATION', message: 'unknown operation' };
    }
  } catch (err) {
    return toMiniappError(err, log);
  }
}

function parseArgv(args) {
  const parsed = {};
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === '--actor') { parsed.actor = args[i + 1]; i += 1; }
    if (args[i] === '--op') { parsed.op = args[i + 1]; i += 1; }
  }
  return parsed;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
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
    process.exit(result?.error_code ? 1 : 0);
  } catch {
    process.stdout.write(`${JSON.stringify({ error_code: 'FAILED', message: 'the toolbox command failed' })}\n`);
    process.exit(1);
  }
}
