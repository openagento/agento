import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { matchesWhitelist } from '../email-match.js';

const here = path.dirname(fileURLToPath(import.meta.url));
// Repo-root fixture, read by the Python parity test too — one table, two gates.
const parityFile = path.join(here, '..', '..', '..', '..', 'tests', 'fixtures', 'email_match_parity.json');
const { cases } = JSON.parse(fs.readFileSync(parityFile, 'utf8'));

describe('matchesWhitelist (shared toolbox matcher)', () => {
  for (const c of cases) {
    it(`${c.why}: ${JSON.stringify(c.patterns)} vs ${JSON.stringify(c.address)} -> ${c.expected}`, () => {
      expect(matchesWhitelist(c.address, c.patterns)).toBe(c.expected);
    });
  }

  it('never throws on a missing address — it returns false', () => {
    for (const bad of [undefined, null, '', '   ']) {
      expect(matchesWhitelist(bad, ['*@corp.com'])).toBe(false);
    }
  });

  it('fails closed on a non-array whitelist', () => {
    for (const bad of [undefined, null, '*@corp.com']) {
      expect(matchesWhitelist('a@corp.com', bad)).toBe(false);
    }
  });

  // The outlook and core gates used to prove "not a replica" by importing this module directly.
  // They cannot: a module tree and the toolbox tree are mounted at unrelated container paths, so a
  // relative import across them fails to load the whole file. They receive THIS function through
  // the registration context instead (config-loader.js TOOLBOX_HELPERS), and
  // module-toolbox-imports.test.js is the guard that keeps a private replica from creeping back.
});
