// Runs the real `agent_run` / `agent_result` / `schedule_followup` handlers, with the real
// `enqueueJob`, against the integration test database. Input on stdin: one call
// {"tool", "args", "jobId", "agentViewId", "moduleConfigs"} or a batch {"calls": [...]}, which runs
// concurrently (Promise.all) on one pool of 20 connections. Prints [{text, isError}] in call order.
import { createRequire, register } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../src/agento');
const load = (rel) => import(pathToFileURL(path.join(root, rel)).href);
const mysql = createRequire(path.join(root, 'toolbox/package.json'))('mysql2/promise');
// A module tree sits outside the toolbox package, so its bare imports (zod) do not resolve by
// walking up; the toolbox image links node_modules for the same reason. Retry them from the toolbox.
const toolboxUrl = pathToFileURL(path.join(root, 'toolbox/package.json')).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s, c, next) {
  try { return await next(s, c); } catch (e) {
    if (e.code !== 'ERR_MODULE_NOT_FOUND' || /^[./]|^[a-z]+:/.test(s)) throw e;
    return next(s, { ...c, parentURL: ${JSON.stringify(toolboxUrl)} });
  }
}`)}`);

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const calls = input.calls ?? [input];

const { enqueueJob } = await load('toolbox/job-enqueue.js');
const { errorCategory } = await load('toolbox/log.js');
const modules = [await load('modules/subagent/toolbox/agent.js'), await load('modules/core/toolbox/schedule.js')];

const pool = mysql.createPool({
  host: process.env.TEST_MYSQL_HOST || 'localhost', port: Number(process.env.TEST_MYSQL_PORT || 3306),
  user: process.env.TEST_MYSQL_USER || 'root', password: process.env.TEST_MYSQL_PASSWORD ?? 'cronagent_root',
  database: process.env.TEST_MYSQL_DATABASE || 'cron_agent_test', connectionLimit: 20,
});
const db = { getCronPool: () => pool };

function handler({ tool, jobId, agentViewId, moduleConfigs }) {
  const tools = {};
  const server = { tool(name, desc, schema, fn) { tools[name] = fn; } };
  const context = {
    log: () => {}, db, isToolEnabled: () => true, jobId, agentViewId, moduleConfigs: moduleConfigs ?? {},
    enqueueJob, errorCategory,
  };
  for (const m of modules) m.register(server, context);
  return tools[tool];
}

try {
  const results = await Promise.all(calls.map(async (call) => {
    const out = await handler(call)(call.args);
    return { text: out.content[0].text, isError: out.isError === true };
  }));
  process.stdout.write(`${JSON.stringify(results)}\n`);
} finally {
  await pool.end();
}
