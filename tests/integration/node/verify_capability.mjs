// Runs the toolbox's real capability verifier, with the real `launch` checker, against the
// integration test database. Input on stdin (a token is a credential, never argv):
// {"token": "...", "endpoint": "invoke"}. Prints the derived auth context as JSON, or null.
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../src/agento');
const load = (rel) => import(pathToFileURL(path.join(root, rel)).href);
const mysql = createRequire(path.join(root, 'toolbox/package.json'))('mysql2/promise');

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const { token, endpoint } = JSON.parse(Buffer.concat(chunks).toString('utf8'));

const { createVerifier, createSourceLookup } = await load('toolbox/capability.js');
const { checkLaunch } = await load('modules/miniapps/toolbox/launch-source.js');
const pool = mysql.createPool({
  host: process.env.TEST_MYSQL_HOST || 'localhost', port: Number(process.env.TEST_MYSQL_PORT || 3306),
  user: process.env.TEST_MYSQL_USER || 'root', password: process.env.TEST_MYSQL_PASSWORD ?? 'cronagent_root',
  database: 'cron_agent_test', connectionLimit: 1,
});
try {
  const verify = createVerifier((sql, params) => pool.query(sql, params),
    { sourceCheckers: createSourceLookup([['launch', checkLaunch]]) });
  process.stdout.write(`${JSON.stringify(await verify(token, { endpoint }))}\n`);
} finally {
  await pool.end();
}
