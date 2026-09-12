import { it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// `src/agento/toolbox/package.json:5` sets `"type": "module"`, so `__dirname`
// does not exist here (`node --input-type=module -e "console.log(typeof __dirname)"`
// prints `undefined`). Derive it from the module URL instead.
const HERE = path.dirname(fileURLToPath(import.meta.url));
const DIR = path.join(HERE, '../../../modules/versioned_artifacts/toolbox');
// A second constant because DIR readdirs `toolbox/` only: the serving container runs
// from `server/`, which is a different process with a different boundary.
const SERVER_DIR = path.join(HERE, '../../../modules/versioned_artifacts/server');

// Only service.js may reach the Git adapter. A tool or CLI that imports the
// backend directly bypasses authorization, limits, locking, recovery, and audit
// — every guarantee this module makes. Backend tests are exempt by living
// outside this directory.
it('only service.js imports git-backend.js', () => {
  const candidates = fs.readdirSync(DIR)
    .filter(f => f.endsWith('.js') && f !== 'service.js' && f !== 'git-backend.js');
  // Without this the whole gate is an assertion about an empty set: a moved or renamed
  // directory readdirs to nothing and the rule passes while it guards nothing.
  expect(candidates.length).toBeGreaterThan(0);
  const offenders = candidates
    .filter(f => /from\s+['"]\.\/git-backend\.js['"]/.test(fs.readFileSync(path.join(DIR, f), 'utf8')));
  expect(offenders).toEqual([]);
});

it('only git-backend.js and git-exec.js reference the git binary', () => {
  const candidates = fs.readdirSync(DIR)
    .filter(f => f.endsWith('.js') && !['git-backend.js', 'git-exec.js'].includes(f));
  expect(candidates.length).toBeGreaterThan(0);
  const offenders = candidates
    .filter(f => /\bspawn\s*\(|\bexecFile\s*\(/.test(fs.readFileSync(path.join(DIR, f), 'utf8')));
  expect(offenders).toEqual([]);
});

// The class the finalize deletion opened: a code the module still publishes in its
// error tables while no path can raise it. It is a text search on purpose, and it is
// bounded to the module's own toolbox sources — the only files that can legitimately
// name an ERROR_CODES member.
it('declares no error code that nothing can throw', () => {
  const sources = fs.readdirSync(DIR)
    .filter(f => f.endsWith('.js') && f !== 'errors.js')
    .map(f => fs.readFileSync(path.join(DIR, f), 'utf8')).join('\n');
  const declared = [...fs.readFileSync(path.join(DIR, 'errors.js'), 'utf8')
    .matchAll(/\b([A-Z_]+): '\1'/g)].map(m => m[1]);
  expect(declared.length).toBeGreaterThan(10);
  expect(declared.filter(c => !sources.includes(`ERROR_CODES.${c}`))).toEqual([]);
});

// The serving container is a static file server. Reaching the store side from it would
// put the Git engine, the audit sink and the DB handle inside a process that answers
// unauthenticated HTTP on a published port.
it('the server imports nothing from the store side and spawns no process', () => {
  const files = fs.readdirSync(SERVER_DIR).filter(f => f.endsWith('.js'));
  expect(files.length).toBeGreaterThan(0);
  const offenders = files.filter(f => {
    const src = fs.readFileSync(path.join(SERVER_DIR, f), 'utf8');
    return /from\s+['"][^'"]*\/(service|git-backend|git-exec|audit|locking)\.js['"]/.test(src)
      || /\bspawn\s*\(|\bexecFile\s*\(/.test(src);
  });
  expect(offenders).toEqual([]);
});

// The server logs request-controlled text — a filename an agent chose, a URL a client
// sent. The defence is structural, not per-call: ONE write site, and it bounds what it
// is given. A second raw `process.stderr.write` added later fails here.
it('the server has one log write site and it bounds its input', () => {
  const src = fs.readFileSync(path.join(SERVER_DIR, 'artifacts-server.js'), 'utf8');
  const writes = src.split('\n').filter(l => /stderr\s*\.\s*write\s*\(/.test(l));
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatch(/boundedLine/);
});
