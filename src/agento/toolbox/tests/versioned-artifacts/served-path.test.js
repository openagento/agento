import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseServedPath } from '../../../modules/versioned_artifacts/server/served-path.js';

// The same fixture web's parse_app_path is tested with (tests/unit/web/test_app_path.py).
const here = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(readFileSync(path.join(here, '../../../../../tests/fixtures/app_path_v1.json'), 'utf8'));

describe('parseServedPath agrees with web', () => {
  it.each(FIXTURE.accept)('accepts the upstream path web returns for $raw', ({ upstream }) => {
    const [, code, , versionId] = upstream.split('/');
    expect(parseServedPath(upstream)).toMatchObject({ kind: 'app', code, versionId });
  });

  it.each(FIXTURE.reject.filter((r) => r.startsWith('/a/')))('refuses %s with the /a prefix removed', (raw) => {
    expect(parseServedPath(raw.slice(2))).toBeNull();
  });

  it('never repairs: a spelling web would have rewritten is refused', () => {
    const rewritten = FIXTURE.accept.map(({ raw, upstream }) => ({ raw: raw.split('?')[0].slice(2), upstream }))
      .filter(({ raw, upstream }) => raw !== upstream);
    expect(rewritten.length).toBeGreaterThan(0);
    for (const { raw } of rewritten) expect(parseServedPath(raw)).toBeNull();
  });

  it('answers / as the healthcheck and nothing else', () => {
    expect(parseServedPath('/')).toEqual({ kind: 'health' });
    expect(parseServedPath('/site/')).toBeNull();
  });
});
