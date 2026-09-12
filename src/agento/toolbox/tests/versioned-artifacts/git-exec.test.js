import { it, expect } from 'vitest';
import { runGit } from '../../../modules/versioned_artifacts/toolbox/git-exec.js';
import { GitFailure } from '../../../modules/versioned_artifacts/toolbox/errors.js';

it('writes stdin and returns stdout as a Buffer', async () => {
  const r = await runGit(['hash-object', '-t', 'blob', '--stdin'], { input: Buffer.from('hello\n') });
  expect(r.stdout.toString().trim()).toMatch(/^[0-9a-f]{40}$/);
});

it('returns raw bytes, not a lossy string', async () => {
  const r = await runGit(['hash-object', '-t', 'blob', '--stdin'], { input: Buffer.from([0xe9, 0xe8]) });
  expect(r.stdout.toString().trim()).toMatch(/^[0-9a-f]{40}$/);
});

it('throws GitFailure carrying the exit status and stderr', async () => {
  const err = await runGit(['cat-file', '-p', 'definitelynotaref'], { cwd: process.cwd() }).catch(e => e);
  expect(err).toBeInstanceOf(GitFailure);
  expect(err.exitCode).toBeGreaterThan(0);
  expect(typeof err.stderr).toBe('string');
});

it('accepts controlled env additions', async () => {
  const r = await runGit(['var', 'GIT_AUTHOR_IDENT'], { env: { GIT_AUTHOR_NAME: 'X', GIT_AUTHOR_EMAIL: 'x@y.z' } });
  expect(r.stdout.toString()).toContain('x@y.z');
});

it('fails closed when output exceeds maxBytes', async () => {
  await expect(runGit(['hash-object', '-t', 'blob', '--stdin'], { input: Buffer.alloc(1024), maxBytes: 4 }))
    .rejects.toBeInstanceOf(GitFailure);
});

it('truncates instead of failing when truncateAt is set', async () => {
  const r = await runGit(['hash-object', '-t', 'blob', '--stdin'], { input: Buffer.alloc(1024), truncateAt: 8 });
  expect(r.truncated).toBe(true);
  expect(r.stdout.length).toBe(8);            // exactly the configured prefix, never more
});

// Round 6. `>=` reported output of EXACTLY the limit as truncated, so a diff whose
// size happened to equal max_diff_bytes was complete but announced as cut short —
// and the docs promise that only diffs PAST the limit are truncated. A boundary
// this test pins because it is the one value the inequality gets wrong.
it('does not call output of exactly truncateAt bytes truncated', async () => {
  // `hash-object` prints a 40-char sha plus a newline: 41 bytes, known exactly.
  const r = await runGit(['hash-object', '-t', 'blob', '--stdin'], { input: Buffer.from('x'), truncateAt: 41 });
  expect(r.stdout.length).toBe(41);
  expect(r.truncated).toBe(false);
});

it('still truncates one byte past the limit', async () => {
  const r = await runGit(['hash-object', '-t', 'blob', '--stdin'], { input: Buffer.from('x'), truncateAt: 40 });
  expect(r.truncated).toBe(true);
  expect(r.stdout.length).toBe(40);
});

it('reports truncated:false when output fits', async () => {
  const r = await runGit(['hash-object', '-t', 'blob', '--stdin'], { input: Buffer.from('x'), truncateAt: 4096 });
  expect(r.truncated).toBe(false);
});

it('refuses maxBytes and truncateAt together', async () => {
  await expect(runGit(['--version'], { maxBytes: 10, truncateAt: 10 })).rejects.toThrow(/mutually exclusive/);
});

it('fails closed on timeout', async () => {
  // `git cat-file --batch` waits on stdin forever when stdin is left open.
  await expect(runGit(['cat-file', '--batch'], { keepStdinOpen: true, timeoutMs: 300 }))
    .rejects.toBeInstanceOf(GitFailure);
});

it('names a spawn failure by its OS code, and that code reaches the operator record', async () => {
  // The one operator-facing fact about a failure that produced no git output at all:
  // `git: not found` on an un-rebuilt toolbox image. A Node/OS code is a fixed
  // identifier from a closed set, so unlike stderr it cannot carry a caller's text.
  const { errorFacts } = await import('../../../modules/versioned_artifacts/toolbox/errors.js');
  const err = await runGit(['status'], { cwd: '/nonexistent-dir-for-spawn-enoent' })
    .then(() => null, (e) => e);
  expect(err?.name).toBe('GitFailure');
  expect(err.code).toBe('ENOENT');
  expect(errorFacts(err)).toBe('GitFailure code=ENOENT exit=-1');
});
