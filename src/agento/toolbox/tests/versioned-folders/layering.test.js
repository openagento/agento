import { it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// `src/agento/toolbox/package.json:5` sets `"type": "module"`, so `__dirname`
// does not exist here (`node --input-type=module -e "console.log(typeof __dirname)"`
// prints `undefined`). Derive it from the module URL instead.
const HERE = path.dirname(fileURLToPath(import.meta.url));
const DIR = path.join(HERE, '../../../modules/versioned_folders/toolbox');

// Only service.js may reach the Git adapter. A tool or CLI that imports the
// backend directly bypasses authorization, limits, locking, recovery, and audit
// — every guarantee this module makes. Backend tests are exempt by living
// outside this directory.
it('only service.js imports git-backend.js', () => {
  const offenders = fs.readdirSync(DIR)
    .filter(f => f.endsWith('.js') && f !== 'service.js' && f !== 'git-backend.js')
    .filter(f => /from\s+['"]\.\/git-backend\.js['"]/.test(fs.readFileSync(path.join(DIR, f), 'utf8')));
  expect(offenders).toEqual([]);
});

it('only git-backend.js and git-exec.js reference the git binary', () => {
  const offenders = fs.readdirSync(DIR)
    .filter(f => f.endsWith('.js') && !['git-backend.js', 'git-exec.js'].includes(f))
    .filter(f => /\bspawn\s*\(|\bexecFile\s*\(/.test(fs.readFileSync(path.join(DIR, f), 'utf8')));
  expect(offenders).toEqual([]);
});
