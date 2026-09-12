// Guards for the defect CLASSES found in implementation review round 1. Each test
// below defends a shape, not the single call site the review named.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile, readFile, chmod, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import fs from 'node:fs';
import path from 'node:path';
import { createService } from '../../../modules/versioned_artifacts/toolbox/service.js';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { toToolError, ArtifactError } from '../../../modules/versioned_artifacts/toolbox/errors.js';
import { readSource } from './helpers.js';
import { execFileSync } from 'node:child_process';

let root, pub, src, svc, rows;
const cfg = (over = {}) => ({ storage_root: root, published_root: pub, allowed_artifacts: 'site',
  'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080', 'limits/max_files': 2000,
  'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'security/allow_symlinks': false, ...over });

const build = (over = {}, opts = {}) => {
  const pool = { execute: async (_sql, params) => { rows.push(params); } };
  return createService({ config: cfg(over), db: { getCronPool: () => pool }, log: vi.fn(),
    jobId: 1, agentViewId: 2, actor: 'admin', ...opts });
};

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'vf-reg-'));
  pub = await mkdtemp(path.join(tmpdir(), 'vf-reg-pub-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-regsrc-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  await mkdir(path.join(src, 'css'));
  await writeFile(path.join(src, 'css', 'style.css'), 'body{}\n');
  rows = [];
  svc = build();
  await svc.init('site', { files: await readSource(src) });
  rows.length = 0;
});
afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(pub, {recursive:true,force:true}); await rm(src, {recursive:true,force:true}); });

const gitDir = () => path.join(root, 'site', 'repo.git');
const refFile = (ref) => path.join(gitDir(), ref);
// A fixture writes where the agent's desk is mirrored to — the draft worktree — and
// commits through the same `commitDraft` a save uses, so no desk and no Linux gate.
const put = (be, draftId, rel, body) =>
  writeFile(path.join(be.getDraftPath(root, 'site', draftId), rel), body);

describe('a damaged store is never reported as a semantic state', () => {
  // The class: a generic storage failure ("could not read") answered with a
  // domain answer ("there is nothing here"). Two independent damage modes, both
  // of which a failed `rev-parse` alone cannot tell from absence.
  it('reports a dangling current pointer as a failure, not as "nothing published"', async () => {
    await writeFile(refFile('refs/agento/current'), `${'de'.repeat(20)}\n`);
    await expect(svc.getCurrent('site')).rejects.toThrow(/STORAGE_OPERATION_FAILED/);
  });

  it('reports an unreadable current pointer as a failure, not as "nothing published"', async () => {
    await writeFile(refFile('refs/agento/current'), 'not-a-sha at all\n');
    await expect(svc.getCurrent('site')).rejects.toThrow(/STORAGE_OPERATION_FAILED/);
  });

  // chmod is only a real permission barrier for a non-root uid, and the plan says as
  // much: its reconcile-failure test injects a backend precisely because "making a
  // directory unreadable is not a reliable thing to do as root in a container". So
  // this one is SKIPPED under root rather than asserted and silently inverted there.
  // The two ref-corruption tests above need no such guard — they write bytes.
  const notRoot = typeof process.getuid !== 'function' || process.getuid() !== 0;

  it.runIf(notRoot)('does not report a draft as discarded while its markers are still in the store', async () => {
    const d = await svc.createDraft('site');
    // Make the ref namespace the teardown must write unwritable. The draft's base
    // marker then survives, and a discard that answers {discarded:true} is exactly
    // the "generic failure read as success" this class is about.
    const nsDir = path.join(root, 'site', 'repo.git', 'refs', 'agento', 'draft-bases');
    await chmod(nsDir, 0o500);
    try {
      await expect(svc.discardDraft('site', d.draft_id)).rejects.toThrow(/STORAGE_OPERATION_FAILED/);
    } finally {
      await chmod(nsDir, 0o700);
    }
    // The base marker teardown could not remove is still there — which is exactly
    // what a `{discarded: true}` answer would have denied.
    const refs = execFileSync('git', ['--git-dir', path.join(root, 'site', 'repo.git'),
      'for-each-ref', '--format=%(refname)', 'refs/agento/draft-bases/'], { encoding: 'utf8' });
    expect(refs).toContain(d.draft_id);
  });
});

describe('the audit boundary covers the whole attempt', () => {
  // The class: a mutation refused BEFORE the backend call leaving no audit row,
  // so the refusals that matter most are the ones the trail cannot show.
  const opOf = (r) => ({ artifact: r[0], operation: r[1], result: r[9], errorCode: r[10] });

  it('records a refused artifact on every mutation, not only a backend failure', async () => {
    const denied = build({ allowed_artifacts: 'site' });
    // `deskFd = -1` never reaches a read: authorization is checked inside `audited`,
    // long before the desk is mirrored.
    await expect(denied.createDraft('other')).rejects.toThrow(/ARTIFACT_ACCESS_DENIED/);
    await expect(denied.publish('other', 'v-20260101-000000-abcd', 'v-20260101-000000-abce'))
      .rejects.toThrow(/ARTIFACT_ACCESS_DENIED/);
    await expect(denied.saveVersion('other', 'd-abcdef', -1, 'x')).rejects.toThrow(/ARTIFACT_ACCESS_DENIED/);
    await expect(denied.discardDraft('other', 'd-abcdef')).rejects.toThrow(/ARTIFACT_ACCESS_DENIED/);
    expect(rows.map(opOf)).toEqual([
      { artifact: 'other', operation: 'versioned_artifact.draft.created', result: 'error', errorCode: 'ARTIFACT_ACCESS_DENIED' },
      { artifact: 'other', operation: 'versioned_artifact.version.published', result: 'error', errorCode: 'ARTIFACT_ACCESS_DENIED' },
      { artifact: 'other', operation: 'versioned_artifact.version.saved', result: 'error', errorCode: 'ARTIFACT_ACCESS_DENIED' },
      { artifact: 'other', operation: 'versioned_artifact.draft.discarded', result: 'error', errorCode: 'ARTIFACT_ACCESS_DENIED' },
    ]);
  });

  it('records a mutation refused because the draft is not there', async () => {
    await expect(svc.saveVersion('site', 'd-abcdef', -1, 'x')).rejects.toThrow(/DRAFT_NOT_FOUND/);
    expect(rows.map(opOf)).toEqual([
      { artifact: 'site', operation: 'versioned_artifact.version.saved', result: 'error', errorCode: 'DRAFT_NOT_FOUND' },
    ]);
  });

  it('writes no row for a malformed identifier, which names nothing to audit', async () => {
    await expect(svc.createDraft('Not A Artifact')).rejects.toThrow(/INVALID_PATH/);
    await expect(svc.saveVersion('site', 'nope', -1, 'x')).rejects.toThrow(/INVALID_PATH/);
    expect(rows).toEqual([]);
  });
});

// ---------------------------------------------------------------- round 2

describe('a caller string never reaches an audit sink unbounded', () => {
  // The class: an audit field whose width is decided by the caller rather than by
  // its SQL column. The row is one sink; `audit-fallback.log` is the other, and it
  // takes the value verbatim when the DB is down.
  it('refuses an oversized identifier before it can be audited at all', async () => {
    const huge = `v-20260101-000000-${'a'.repeat(5000)}`;
    await expect(svc.publish('site', huge, huge)).rejects.toMatchObject({ code: 'INVALID_PATH' });
    expect(rows).toEqual([]);                      // nothing auditable was named
    await expect(readFile(path.join(root, 'audit-fallback.log'), 'utf8')).rejects.toThrow();
  });

  it('caps every field to its own column width in both sinks', async () => {
    const dead = build({}, { db: { getCronPool: () => ({ execute: async () => { throw new Error('db down'); } }) } });
    await dead.createDraft('site', 'current', 'M'.repeat(400));
    const line = JSON.parse((await readFile(path.join(root, 'audit-fallback.log'), 'utf8')).trim().split('\n').pop());
    expect(line.description.length).toBe(255);     // VARCHAR(255)
    expect(line.operation.length).toBeLessThanOrEqual(48);
    expect(line.draftId.length).toBeLessThanOrEqual(64);
  });

  it('bounds the identifiers at the tool schema, not only in the service', async () => {
    const mod = await import('../../../modules/versioned_artifacts/toolbox/paths.js');
    expect(mod.VERSION_ID_RE.test('v-20260101-000000-abcd')).toBe(true);
    expect(mod.VERSION_ID_RE.test(`v-20260101-000000-${'a'.repeat(5000)}`)).toBe(false);
    expect(mod.DRAFT_ID_RE.test('d-abcdef')).toBe(true);
    expect(mod.DRAFT_ID_RE.test(`d-${'a'.repeat(5000)}`)).toBe(false);
  });
});

describe('a failure is never read as a proven state (round 2 instances)', () => {
  // Provoked with ENOTDIR — a file standing where a directory must be — because it
  // is uid-independent, unlike chmod. `stat()` on a path THROUGH a file fails with
  // ENOTDIR, which says "unknown", not "absent".
  it('does not report a draft as missing when the drafts path cannot be walked', async () => {
    await rm(path.join(root, 'site', 'worktrees'), { recursive: true, force: true });
    await writeFile(path.join(root, 'site', 'worktrees'), 'not a directory');
    await expect(svc.saveVersion('site', 'd-abcdef', -1, 'x'))
      .rejects.toMatchObject({ code: 'STORAGE_OPERATION_FAILED' });
  });
});

// ---------------------------------------------------------------- round 3

describe('recovery resets the index, not only the working tree (round 3)', () => {
  // The class: a cleanup that restores FROM the index and therefore cannot undo a
  // change TO the index. A file staged before a crash survives `checkout -- .` +
  // `clean -fdx` and rides along in the NEXT commit — a file the caller was told had
  // failed, in a revision that never listed it.
  it('reports the staged file as gone from an explicit recovery too', async () => {
    const d = await svc.createDraft('site');
    const dir = path.join(root, 'site', 'worktrees', d.draft_id);
    await writeFile(path.join(dir, 'leaked.txt'), 'x\n');
    execFileSync('git', ['-C', dir, 'add', 'leaked.txt']);
    const be = createBackend();
    expect(await be.recoverDraft(root, 'site', d.draft_id)).toEqual({ recovered: true });
    expect(execFileSync('git', ['-C', dir, 'status', '--porcelain'], { encoding: 'utf8' }).trim()).toBe('');
  });
});

describe('startup reclamation is maintenance, not authorization (round 3)', () => {
  // The class: an internal maintenance pass gated on an authorization setting. The
  // allowlist is agent_view-scoped and the toolbox startup pass resolves only the
  // default scope, so with the normal empty default the reclamation loop had no
  // artifacts to walk at all — and an artifact removed from every allowlist kept its
  // orphans forever.
  it('reclaims an orphan in an artifact no allowlist names', async () => {
    const orphan = path.join(root, 'site', 'worktrees', 'd-deadbe');
    await mkdir(orphan, { recursive: true });
    const closed = build({ allowed_artifacts: '' });
    expect(await closed.startupSweep()).toMatchObject({ drafts: 1 });
    await expect(readFile(path.join(orphan, '.git'), 'utf8')).rejects.toThrow();

    // And the allowlist still denies the caller — reclamation added no second way in.
    await expect(closed.getCurrent('site')).rejects.toMatchObject({ code: 'ARTIFACT_ACCESS_DENIED' });
  });

  it('walks only directories that are really artifacts', async () => {
    await mkdir(path.join(root, 'Not-A-Code'), { recursive: true });     // uppercase: not an artifact_code
    await mkdir(path.join(root, 'empty-dir'), { recursive: true });      // no store inside
    await writeFile(path.join(root, 'stray-file'), 'x');
    expect(await createBackend().listArtifacts(root)).toEqual(['site']);
  });
});

describe('an internal failure reaches the operator log, never the caller (round 3)', () => {
  // The class: diagnostic context attached to an error and never read by anything.
  // Thirteen sites carried `{cause}`; the boundary returned before touching it.
  // Round 8 narrowed what this record may hold: a class and an exit status, never
  // git's own words. Git echoes the pathspec it was given, so its stderr is caller
  // text in disguise — see `errorFacts`. The record still proves the cause was READ.
  it('logs one bounded cause line for a storage failure and keeps the response generic', async () => {
    const { toToolError, ArtifactError, GitFailure, ERROR_CODES } =
      await import('../../../modules/versioned_artifacts/toolbox/errors.js');
    const log = vi.fn();
    const cause = new GitFailure(128, `fatal: cannot lock ref\n${'x'.repeat(500)}`);
    const res = toToolError(new ArtifactError(ERROR_CODES.PUBLISH_FAILED, 'could not move current', { cause }), log);
    expect(res).toEqual({ error_code: 'PUBLISH_FAILED', message: 'could not move current' });
    const [[, level, details]] = log.mock.calls;
    expect(level).toBe('ERROR');
    expect(details).toBe('PUBLISH_FAILED: could not move current — cause: GitFailure exit=128');
    expect(details).not.toContain('cannot lock ref');
    expect(details.split('\n')).toHaveLength(1);
    expect(details.length).toBeLessThan(300);
  });

  it('says nothing about a version the caller simply does not have', async () => {
    const { toToolError, ArtifactError, ERROR_CODES } =
      await import('../../../modules/versioned_artifacts/toolbox/errors.js');
    const log = vi.fn();
    toToolError(new ArtifactError(ERROR_CODES.VERSION_NOT_FOUND, 'version not found', { cause: new Error('boom') }), log);
    expect(log).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------- round 4

describe('one file has exactly one name (round 4)', () => {
  // The class: the canonical path the validator RETURNED was thrown away and
  // re-derived by the caller with `split(path.sep)`. On Linux `path.sep` is "/", so
  // every alias of a path — "a//b.txt", "a/./b.txt", "a\\b.txt" — kept its original
  // spelling where containment had resolved it to one file. The agent's own paths
  // now come from the desk, where the filesystem decides the name; what is left is
  // the admin CLI, which still takes source paths a human typed.
  it('imports an aliased source path under its canonical name', async () => {
    const svc2 = build({ allowed_artifacts: 'site,alias' });
    await svc2.init('alias', { files: [{ path: 'a//deep/./f.txt', content: 'ok', encoding: 'utf-8' }] });
    const { current_version } = await svc2.getCurrent('alias');
    const names = execFileSync('git', ['--git-dir', path.join(root, 'alias', 'repo.git'),
      'ls-tree', '-r', '--name-only', '-z', `refs/agento/versions/${current_version}`],
      { encoding: 'utf8' }).split('\0').filter(Boolean);
    expect(names).toEqual(['a/deep/f.txt']);
  });
});

const nextSecond = () => {
  const s = new Date().getUTCSeconds();
  return new Promise((res) => {
    const t = setInterval(() => { if (new Date().getUTCSeconds() !== s) { clearInterval(t); res(); } }, 20);
  });
};

describe('"newest first" means newest SAVED (round 4)', () => {
  // The class: a listing ordered by a Git-derived timestamp instead of by the domain
  // event it claims to order. Version refs are lightweight, so `creatordate` is the
  // date of the COMMIT — which can predate the save by any amount. Saving an older
  // draft last therefore listed it second, and `limit: 1` omitted the newest version.
  it('lists the last-saved version first even when its commit is older', async () => {
    const be = createBackend();
    const a = await svc.createDraft('site');
    await put(be, a.draft_id, 'a.txt', 'a');
    await be.commitDraft(root, 'site', a.draft_id, 'edit a', {});
    // Across a second boundary, deliberately: the two commits must differ in
    // creatordate, or a listing sorted by it would pass this test on a tie.
    await nextSecond();
    const b = await svc.createDraft('site');
    await put(be, b.draft_id, 'b.txt', 'b');
    await be.commitDraft(root, 'site', b.draft_id, 'edit b', {});

    // Minting needs a desk, so the two version refs are a FIXTURE — byte-for-byte
    // what `saveVersion` writes. The OLDER commit gets the NEWER id, which is the
    // whole point: only a listing that reads the id can order these two correctly.
    const tip = (id) => execFileSync('git', ['--git-dir', gitDir(), 'rev-parse',
      `refs/heads/agento-drafts/${id}`], { encoding: 'utf8' }).trim();
    const older = 'v-29990101-000000-aaaa';
    const newer = 'v-29990101-000001-bbbb';
    execFileSync('git', ['--git-dir', gitDir(), 'update-ref', `refs/agento/versions/${older}`, tip(b.draft_id)]);
    execFileSync('git', ['--git-dir', gitDir(), 'update-ref', `refs/agento/versions/${newer}`, tip(a.draft_id)]);

    const listed = (await svc.listVersions('site')).map((v) => v.version_id);
    expect(listed[0]).toBe(newer);
    expect((await svc.listVersions('site', { limit: 1 })).map((v) => v.version_id)).toEqual([newer]);
  });
});

describe('a sweep that could not enumerate the store is not a completed sweep (round 4)', () => {
  // The class: a failure that is logged and then reported to the caller as a
  // successful empty result. The caller's OK line ("reclaimed 0 draft(s)") then
  // contradicted the ERROR line above it, and that OK is the operator's last word.
  it('fails the startup sweep instead of reporting zero artifacts', async () => {
    // The enumeration is broken on its own, with the lock sweep left working: a
    // chmod on the store would fail the lock sweep first and prove nothing about
    // this path.
    const be = createBackend();
    const blind = { ...be, listArtifacts: async () => { throw new Error('store unreadable'); } };
    await expect(build({}, { backend: blind }).startupSweep()).rejects.toThrow(/store unreadable/);

    // And a single damaged ARTIFACT still does not fail the whole sweep.
    const oneBad = { ...be, reconcileDrafts: async (r, c) => { if (c === 'site') throw new Error('damaged'); return []; } };
    expect(await build({}, { backend: oneBad }).startupSweep()).toMatchObject({ drafts: 0 });
  });
});

// ---------------------------------------------------------------- round 5
// From the forced sweep of service.js (flagged by the loop's findings ledger in
// every round so far) for the round-4 shape: a failure degraded into a benign
// value. The remaining site is the audit pool accessor, and the degradation there
// is correct — but only because the row still reaches a sink. That is what this
// guards; the existing fallback tests all break `execute`, never the ACCESSOR.
describe('an unreachable DB still records the audit event', () => {
  it('keeps the row when getCronPool itself throws, not only when the INSERT does', async () => {
    const log = vi.fn();
    const blind = build({}, { db: { getCronPool: () => { throw new Error('pool exhausted'); } }, log });
    const d = await blind.createDraft('site', 'current', 'no db');
    expect(d.draft_id).toMatch(/^d-/);                       // the mutation still succeeds
    const line = JSON.parse((await readFile(path.join(root, 'audit-fallback.log'), 'utf8')).trim().split('\n').pop());
    expect(line.operation).toContain('draft.created');
    expect(line.result).toBe('ok');
    // And the operator is told, so the fallback is not a silent substitution.
    expect(log.mock.calls.some(([, lvl, msg]) => lvl === 'ERROR' && /audit insert failed/.test(msg))).toBe(true);
  });
});

// ---------------------------------------------------------------- round 6
// The class: an agent-controlled string reaching a persistent log record without
// bounding or normalization. `save_version` puts the agent's change message in
// argv, GitFailure carried argv in its `message`, and toToolError's
// unexpected-error branch interpolated that message raw into toolbox_mcp.log — so
// one newline in a change message wrote a log record of the agent's choosing.
describe('an agent-supplied change message cannot write the operator log', () => {
  const evil = () => `SECRET-SENTINEL${String.fromCharCode(10)}`
    + `[2020-01-01T00:00:00Z] [versioned_artifacts] OK forged-by-the-agent`
    + String.fromCharCode(0);
  const linux = process.platform === 'linux';

  it.skipIf(!linux)('keeps every record one bounded line with none of the agent\'s text', async () => {
    // The description reaches argv only AFTER the desk is mirrored into the draft,
    // and `mirrorOut` is fd-anchored — so this one needs a real desk descriptor.
    const log = vi.fn();
    const s = build({}, { log });
    const d = await s.createDraft('site', 'current', 'x');
    const desk = await mkdtemp(path.join(tmpdir(), 'vf-reg-desk-'));
    await writeFile(path.join(desk, 'a.txt'), 'a');
    const fd = fs.openSync(desk, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
    // A NUL in argv makes spawn() throw before git starts, which is the cheapest
    // way to reach the failure path with a message the agent chose.
    let caught;
    try { await s.saveVersion('site', d.draft_id, fd, evil()); }
    catch (err) { caught = err; }
    finally { fs.closeSync(fd); await rm(desk, { recursive: true, force: true }); }

    // The backend contract: a GitFailure never leaves the service.
    expect(caught).toBeInstanceOf(ArtifactError);
    expect(caught.code).toBe('STORAGE_OPERATION_FAILED');

    const resp = toToolError(caught, log);
    expect(resp.message).not.toMatch(/SECRET-SENTINEL/);

    const records = log.mock.calls.filter(([, lvl]) => lvl === 'ERROR').map(([, , msg]) => String(msg));
    expect(records.length).toBeGreaterThan(0);
    for (const r of records) {
      expect(r).not.toMatch(/SECRET-SENTINEL/);           // agent text never reaches the log
      expect(r).not.toMatch(/forged-by-the-agent/);
      expect(r).not.toContain(String.fromCharCode(10));   // one record, not several
      expect(r).not.toContain(String.fromCharCode(0));    // \s does not match NUL
      expect(r.length).toBeLessThanOrEqual(300);          // bounded whatever produced it
    }
  });

  it('never carries the argument list, whatever the command was', async () => {
    // The narrowest statement of the defect: a GitFailure raised by a REAL failing
    // git command must not quote argv, because argv holds the change message. Run
    // one outside a repository so git fails for its own reasons.
    const { runGit } = await import('../../../modules/versioned_artifacts/toolbox/git-exec.js');
    const err = await runGit(['commit', '-m', 'SECRET-SENTINEL'], { cwd: tmpdir() })
      .then(() => null, (e) => e);
    expect(err?.name).toBe('GitFailure');
    expect(err.message).not.toMatch(/SECRET-SENTINEL/);
    expect(String(err.stderr)).not.toMatch(/SECRET-SENTINEL/);
    expect(err.message).not.toMatch(/commit/);              // no argv at all, not just no message
  });

  it('records the failure code in the audit row instead of null', async () => {
    // Injected so the net is tested where it actually applies: a backend that
    // re-throws a RAW GitFailure, which is what any unmapped gitWork call in a
    // mutation path does. Without the mapping the audit row's error_code is null —
    // the single record of the failure would not say what failed. The stub throws
    // before any desk is read, so the descriptor is never used.
    const { GitFailure } = await import('../../../modules/versioned_artifacts/toolbox/errors.js');
    const real = createBackend();
    const raw = { ...real, saveVersion: async () => { throw new GitFailure(128, 'fatal: disk full'); } };
    const s = build({}, { backend: raw });
    const d = await s.createDraft('site', 'current', 'x');
    rows.length = 0;
    const caught = await s.saveVersion('site', d.draft_id, -1, 'm').then(() => null, (e) => e);
    expect(caught).toBeInstanceOf(ArtifactError);                 // never a GitFailure at the boundary
    expect(caught.code).toBe('STORAGE_OPERATION_FAILED');
    const row = rows.at(-1);
    expect(row).toBeTruthy();
    expect(row.some((v) => v === 'error')).toBe(true);
    expect(row.some((v) => v === 'STORAGE_OPERATION_FAILED')).toBe(true);
  });

  it('never formats a free-form error message into a log record, anywhere', async () => {
    // The class, asserted STRUCTURALLY. Round 6 bounded these records, which stopped
    // the forgery but not the disclosure: an exception message is written by whoever
    // threw it, and a DB driver quotes the offending value back ("Incorrect string
    // value … for column 'description'" — and that description is agent-supplied).
    // So the rule is now a class and a machine code, never `.message`. Seven sites
    // in five files; git-exec's `error` handler was the one no review named, found by
    // sweeping for the shape rather than by re-reading the finding.
    const dir = new URL('../../../modules/versioned_artifacts/toolbox/', import.meta.url);
    for (const file of ['errors.js', 'audit.js', 'service.js', 'versioned-artifacts.js', 'git-exec.js']) {
      const src = await readFile(new URL(file, dir), 'utf8');
      src.split('\n').forEach((line, i) => {
        if (line.trimStart().startsWith('*') || line.trimStart().startsWith('//')) return;
        expect(line, `${file}:${i + 1} formats a raw error message`)
          .not.toMatch(/(err|err2|e)\??\.message/);
      });
    }
  });

  it('does not let a foreign error masquerade as a GitFailure to unlock fields', async () => {
    // `errorFacts` used a string equality on `err.name`, so any thrower could set
    // name:'GitFailure' and hand over its own free-form `stderr`. The class is checked
    // with instanceof, and a `code` is accepted only if it looks like a machine code.
    const log = vi.fn();
    const fake = new Error('boom');
    fake.name = 'GitFailure';
    fake.stderr = 'SECRET-SENTINEL from a foreign error';
    fake.code = 'leaking free-form text pretending to be a code: SECRET-SENTINEL';
    fake.exitCode = 'SECRET-SENTINEL';
    toToolError(fake, log);
    const record = String(log.mock.calls[0][2]);
    expect(record).not.toMatch(/SECRET-SENTINEL/);
    expect(record).not.toMatch(/exit=/);          // a non-integer exit status is dropped
    expect(record).toMatch(/internal error: GitFailure/);   // the name itself is harmless
  });

  it('discloses no caller text even when the failing library quotes it', async () => {
    // The two routes the review reproduced: an alien error whose own message carries
    // the sentinel, and a DB driver that quotes the agent-supplied description back.
    const log = vi.fn();
    toToolError(new Error('foreign failure contains SECRET-SENTINEL'), log);
    expect(log.mock.calls.map(([, , m]) => String(m)).join('|')).not.toMatch(/SECRET-SENTINEL/);

    const quoting = {
      execute: async (_sql, params) => {
        const err = new Error(`Incorrect string value for column 'description': ${params[11]}`);
        err.code = 'ER_TRUNCATED_WRONG_VALUE';
        throw err;
      },
    };
    const log2 = vi.fn();
    const s = build({}, { db: { getCronPool: () => quoting }, log: log2 });
    await s.createDraft('site', 'current', 'SECRET-SENTINEL in the description');
    const records = log2.mock.calls.filter(([, lvl]) => lvl === 'ERROR').map(([, , m]) => String(m));
    expect(records.length).toBeGreaterThan(0);
    expect(records.join('|')).not.toMatch(/SECRET-SENTINEL/);
    // The diagnostic an operator acts on survives: the class and the machine code.
    expect(records.join('|')).toMatch(/ER_TRUNCATED_WRONG_VALUE/);
  });

  it('bounds AND withholds an unexpected error that is not ours at all', async () => {
    // The generic branch, reached directly: its input is by definition an error this
    // module did not build, so neither its length nor its CONTENT is ours to trust.
    const log = vi.fn();
    const alien = new Error(`boom SECRET-SENTINEL${String.fromCharCode(10)}[forged] OK${String.fromCharCode(0)}${'x'.repeat(500)}`);
    alien.code = 'EACCES';
    toToolError(alien, log);
    const [[, lvl, msg]] = log.mock.calls;
    expect(lvl).toBe('ERROR');
    expect(String(msg)).not.toMatch(/SECRET-SENTINEL/);     // disclosure, not only forgery
    expect(String(msg)).not.toContain(String.fromCharCode(10));
    expect(String(msg)).not.toContain(String.fromCharCode(0));
    expect(String(msg).length).toBeLessThanOrEqual(200);
    expect(String(msg)).toMatch(/EACCES/);                  // the useful half is kept
  });
});
