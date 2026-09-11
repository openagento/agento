// Guards for the defect CLASSES found in implementation review round 1. Each test
// below defends a shape, not the single call site the review named.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile, readFile, chmod, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createService } from '../../../modules/versioned_folders/toolbox/service.js';
import { createBackend } from '../../../modules/versioned_folders/toolbox/git-backend.js';
import { toToolError, VfError } from '../../../modules/versioned_folders/toolbox/errors.js';
import { readSource, draftDirs } from './helpers.js';
import { execFileSync } from 'node:child_process';

let root, src, svc, rows;
const cfg = (over = {}) => ({ storage_root: root, allowed_folders: 'site', 'limits/max_files': 2000,
  'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600, 'limits/max_diff_bytes': 1048576,
  'security/allow_symlinks': false, ...over });

const build = (over = {}, opts = {}) => {
  const pool = { execute: async (_sql, params) => { rows.push(params); } };
  return createService({ config: cfg(over), db: { getCronPool: () => pool }, log: vi.fn(),
    jobId: 1, agentViewId: 2, actor: 'admin', ...opts });
};

beforeEach(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'vf-reg-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-regsrc-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  await mkdir(path.join(src, 'css'));
  await writeFile(path.join(src, 'css', 'style.css'), 'body{}\n');
  rows = [];
  svc = build();
  await svc.init('site', { files: await readSource(src) });
  rows.length = 0;
});
afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(src, {recursive:true,force:true}); });

const refFile = (ref) => path.join(root, 'site', 'repo.git', ref);

describe('a damaged store is never reported as a semantic state', () => {
  // The class: a generic storage failure ("could not read") answered with a
  // domain answer ("there is nothing here"). Two independent damage modes, both
  // of which a failed `rev-parse` alone cannot tell from absence.
  it('reports a dangling current pointer as a failure, not as "nothing published"', async () => {
    await writeFile(refFile('refs/agento/current'), `${'de'.repeat(20)}\n`);
    await expect(svc.getCurrent('site')).rejects.toThrow(/GIT_OPERATION_FAILED/);
  });

  it('reports an unreadable current pointer as a failure, not as "nothing published"', async () => {
    await writeFile(refFile('refs/agento/current'), 'not-a-sha at all\n');
    await expect(svc.getCurrent('site')).rejects.toThrow(/GIT_OPERATION_FAILED/);
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
      await expect(svc.discardDraft('site', d.draft_id)).rejects.toThrow(/GIT_OPERATION_FAILED/);
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

describe('read paths validate the caller path exactly as write paths do', () => {
  // The class: an LLM-supplied path reaching the store without the containment
  // check, and being answered with a domain result instead of INVALID_PATH.
  const bad = ['../escape', 'a/../../escape', '/etc/passwd', '.git/config'];

  it('rejects a traversing or metadata path on read_file', async () => {
    const v = await svc.getCurrent('site');
    for (const p of bad) {
      await expect(svc.readFile('site', { versionId: v.current_version }, p)).rejects.toThrow(/INVALID_PATH/);
    }
  });

  it('rejects a traversing or metadata path on list_files', async () => {
    const v = await svc.getCurrent('site');
    for (const p of bad) {
      await expect(svc.listFiles('site', { versionId: v.current_version, path: p })).rejects.toThrow(/INVALID_PATH/);
    }
    // A legitimate prefix still filters.
    const files = await svc.listFiles('site', { versionId: v.current_version, path: 'css' });
    expect(files.map((f) => f.path)).toEqual(['css/style.css']);
  });
});

describe('the audit boundary covers the whole attempt', () => {
  // The class: a mutation refused BEFORE the backend call leaving no audit row,
  // so the refusals that matter most are the ones the trail cannot show.
  const opOf = (r) => ({ folder: r[0], operation: r[1], result: r[9], errorCode: r[10] });

  it('records a refused folder on every mutation, not only a backend failure', async () => {
    const denied = build({ allowed_folders: 'site' });
    await expect(denied.createDraft('other')).rejects.toThrow(/FOLDER_ACCESS_DENIED/);
    await expect(denied.publish('other', 'v-20260101-000000-abcd', 'v-20260101-000000-abce'))
      .rejects.toThrow(/FOLDER_ACCESS_DENIED/);
    await expect(denied.applyChanges('other', 'd-abcdef', [])).rejects.toThrow(/FOLDER_ACCESS_DENIED/);
    await expect(denied.finalize('other', 'd-abcdef')).rejects.toThrow(/FOLDER_ACCESS_DENIED/);
    await expect(denied.discardDraft('other', 'd-abcdef')).rejects.toThrow(/FOLDER_ACCESS_DENIED/);
    expect(rows.map(opOf)).toEqual([
      { folder: 'other', operation: 'versioned_folder.draft.created', result: 'error', errorCode: 'FOLDER_ACCESS_DENIED' },
      { folder: 'other', operation: 'versioned_folder.version.published', result: 'error', errorCode: 'FOLDER_ACCESS_DENIED' },
      { folder: 'other', operation: 'versioned_folder.draft.changed', result: 'error', errorCode: 'FOLDER_ACCESS_DENIED' },
      { folder: 'other', operation: 'versioned_folder.version.finalized', result: 'error', errorCode: 'FOLDER_ACCESS_DENIED' },
      { folder: 'other', operation: 'versioned_folder.draft.discarded', result: 'error', errorCode: 'FOLDER_ACCESS_DENIED' },
    ]);
  });

  it('records a mutation refused because the draft is not there', async () => {
    await expect(svc.applyChanges('site', 'd-abcdef', [{ path: 'x', content: 'y' }]))
      .rejects.toThrow(/DRAFT_NOT_FOUND/);
    expect(rows.map(opOf)).toEqual([
      { folder: 'site', operation: 'versioned_folder.draft.changed', result: 'error', errorCode: 'DRAFT_NOT_FOUND' },
    ]);
  });

  it('writes no row for a malformed identifier, which names nothing to audit', async () => {
    await expect(svc.createDraft('Not A Folder')).rejects.toThrow(/INVALID_PATH/);
    await expect(svc.applyChanges('site', 'nope', [])).rejects.toThrow(/INVALID_PATH/);
    expect(rows).toEqual([]);
  });
});

describe('finalize converges without asking the caller to retry', () => {
  it('retries a resumable teardown once, inside the same lock', async () => {
    let failures = 0;
    const flaky = createBackend({
      hooks: {
        afterTeardownStep: (step) => {
          // Fail the FIRST teardown attempt only: the version and its marker are
          // already written by then, so the second attempt resumes and finishes.
          if (step === 'branch' && failures === 0) { failures += 1; throw new Error('transient teardown failure'); }
        },
      },
    });
    const s = build({}, { backend: flaky });
    const d = await s.createDraft('site');
    await s.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'x' }], [], 'add');
    const v = await s.finalize('site', d.draft_id, 'done');
    expect(v.version_id).toMatch(/^v-/);
    expect(failures).toBe(1);
    expect(await draftDirs(root, 'site')).not.toContain(d.draft_id);
    const versions = await s.listVersions('site');
    expect(versions.map((x) => x.version_id)).toContain(v.version_id);
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
    const d = await dead.createDraft('site');
    await dead.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'x' }], [], 'batch');
    await dead.finalize('site', d.draft_id, 'M'.repeat(400));
    const line = JSON.parse((await readFile(path.join(root, 'audit-fallback.log'), 'utf8')).trim().split('\n').pop());
    expect(line.description.length).toBe(255);     // VARCHAR(255)
    expect(line.operation.length).toBeLessThanOrEqual(48);
    expect(line.draftId.length).toBeLessThanOrEqual(64);
  });

  it('bounds the identifiers at the tool schema, not only in the service', async () => {
    const mod = await import('../../../modules/versioned_folders/toolbox/paths.js');
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
    await expect(svc.applyChanges('site', 'd-abcdef', [{ path: 'a', content: 'b' }], [], 'x'))
      .rejects.toMatchObject({ code: 'GIT_OPERATION_FAILED' });
  });

  it('does not report a file as absent when its object is missing from the store', async () => {
    const d = await svc.createDraft('site');
    await svc.applyChanges('site', d.draft_id, [{ path: 'only.txt', content: 'payload\n' }], [], 'add');
    const v = await svc.finalize('site', d.draft_id, 'v');
    const gitdir = path.join(root, 'site', 'repo.git');
    const sha = execFileSync('git', ['--git-dir', gitdir, 'rev-parse', `refs/agento/versions/${v.version_id}:only.txt`],
      { encoding: 'utf8' }).trim();
    // The tree still names the path; only the blob is gone. That is damage, not a
    // file the caller never wrote.
    await rm(path.join(gitdir, 'objects', sha.slice(0, 2), sha.slice(2)), { force: true });
    await expect(svc.readFile('site', { versionId: v.version_id }, 'only.txt'))
      .rejects.toMatchObject({ code: 'GIT_OPERATION_FAILED' });
  });
});

// ---------------------------------------------------------------- round 3

describe('recovery resets the index, not only the working tree (round 3)', () => {
  // The class: a cleanup that restores FROM the index and therefore cannot undo a
  // change TO the index. `git add` has already run by the time a batch fails, so
  // the staged entries survive `checkout -- .` + `clean -fdx` and ride along in the
  // NEXT batch's commit — a file the caller was told had failed, in a revision that
  // never listed it.
  it('keeps a staged file out of the next revision', async () => {
    const d = await svc.createDraft('site');
    const dir = path.join(root, 'site', 'worktrees', d.draft_id);
    await writeFile(path.join(dir, 'leaked.txt'), 'staged by a crashed batch\n');
    execFileSync('git', ['-C', dir, 'add', 'leaked.txt']);
    expect(execFileSync('git', ['-C', dir, 'status', '--porcelain'], { encoding: 'utf8' })).toMatch(/A\s+leaked\.txt/);

    // The next mutation recovers the draft first (service.prepareDraft), so a
    // recovery that leaves the index dirty commits both files here.
    const r = await svc.applyChanges('site', d.draft_id, [{ path: 'next.txt', content: 'wanted\n' }], [], 'next');
    expect(r.changed).toEqual(['next.txt']);
    const names = (await svc.listFiles('site', { draftId: d.draft_id })).map((f) => f.path);
    expect(names).toContain('next.txt');
    expect(names).not.toContain('leaked.txt');          // the revision, not just the worktree
    expect(execFileSync('git', ['-C', dir, 'status', '--porcelain'], { encoding: 'utf8' }).trim()).toBe('');
  });

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
  // folders to walk at all — and a folder removed from every allowlist kept its
  // orphans forever.
  it('reclaims an orphan in a folder no allowlist names', async () => {
    const orphan = path.join(root, 'site', 'worktrees', 'd-deadbe');
    await mkdir(orphan, { recursive: true });
    const closed = build({ allowed_folders: '' });
    expect(await closed.startupSweep()).toMatchObject({ drafts: 1 });
    await expect(readFile(path.join(orphan, '.git'), 'utf8')).rejects.toThrow();

    // And the allowlist still denies the caller — reclamation added no second way in.
    await expect(closed.getCurrent('site')).rejects.toMatchObject({ code: 'FOLDER_ACCESS_DENIED' });
  });

  it('walks only directories that are really folders', async () => {
    await mkdir(path.join(root, 'Not-A-Code'), { recursive: true });     // uppercase: not a folder_code
    await mkdir(path.join(root, 'empty-dir'), { recursive: true });      // no store inside
    await writeFile(path.join(root, 'stray-file'), 'x');
    expect(await createBackend().listFolders(root)).toEqual(['site']);
  });
});

describe('a probe result is decided on bytes, not on text (round 3)', () => {
  // The class: a probe whose answer decides absence, filtered through text
  // normalization first. `-z` output for a match is `name\0`; stripping the NUL and
  // trimming turns the valid filename " " into "" and reads damage as absence.
  it('reports a damaged blob for a whitespace-only filename as damage', async () => {
    const d = await svc.createDraft('site');
    await svc.applyChanges('site', d.draft_id, [{ path: ' ', content: 'payload\n' }], [], 'odd name');
    const v = await svc.finalize('site', d.draft_id, 'v');
    const gitdir = path.join(root, 'site', 'repo.git');
    const sha = execFileSync('git', ['--git-dir', gitdir, 'rev-parse', `refs/agento/versions/${v.version_id}: `],
      { encoding: 'utf8' }).trim();
    await rm(path.join(gitdir, 'objects', sha.slice(0, 2), sha.slice(2)), { force: true });
    await expect(svc.readFile('site', { versionId: v.version_id }, ' '))
      .rejects.toMatchObject({ code: 'GIT_OPERATION_FAILED' });
  });
});

describe('one batch, one outcome per path (round 3)', () => {
  // The class: a batch whose two arrays can name the same path, resolved in one
  // order by the projected-state check and in the opposite order on disk. The
  // response then reports the path as both changed and deleted.
  const unchanged = async (draftId) =>
    (await svc.readFile('site', { draftId }, 'index.html')).content;

  it('refuses a batch that writes and deletes the same path', async () => {
    const d = await svc.createDraft('site');
    await expect(svc.applyChanges('site', d.draft_id,
      [{ path: 'index.html', content: '<h1>v2</h1>\n' }], ['index.html'], 'both'))
      .rejects.toMatchObject({ code: 'INVALID_PATH' });
    expect(await unchanged(d.draft_id)).toBe('<h1>v1</h1>\n');   // nothing touched disk
  });

  it('refuses a duplicated write and a duplicated delete', async () => {
    const d = await svc.createDraft('site');
    await expect(svc.applyChanges('site', d.draft_id,
      [{ path: 'a.txt', content: '1' }, { path: 'a.txt', content: '2' }], [], 'twice'))
      .rejects.toMatchObject({ code: 'INVALID_PATH' });
    await expect(svc.applyChanges('site', d.draft_id, [], ['index.html', 'index.html'], 'twice'))
      .rejects.toMatchObject({ code: 'INVALID_PATH' });
    expect(await unchanged(d.draft_id)).toBe('<h1>v1</h1>\n');
  });
});

describe('an internal failure reaches the operator log, never the caller (round 3)', () => {
  // The class: diagnostic context attached to an error and never read by anything.
  // Thirteen sites carried `{cause}`; the boundary returned before touching it.
  // Round 8 narrowed what this record may hold: a class and an exit status, never
  // git's own words. Git echoes the pathspec it was given, so its stderr is caller
  // text in disguise — see `errorFacts`. The record still proves the cause was READ.
  it('logs one bounded cause line for a storage failure and keeps the response generic', async () => {
    const { toToolError, VfError, GitFailure, ERROR_CODES } =
      await import('../../../modules/versioned_folders/toolbox/errors.js');
    const log = vi.fn();
    const cause = new GitFailure(128, `fatal: cannot lock ref\n${'x'.repeat(500)}`);
    const res = toToolError(new VfError(ERROR_CODES.PUBLISH_FAILED, 'could not move current', { cause }), log);
    expect(res).toEqual({ error_code: 'PUBLISH_FAILED', message: 'could not move current' });
    const [[, level, details]] = log.mock.calls;
    expect(level).toBe('ERROR');
    expect(details).toBe('PUBLISH_FAILED: could not move current — cause: GitFailure exit=128');
    expect(details).not.toContain('cannot lock ref');
    expect(details.split('\n')).toHaveLength(1);
    expect(details.length).toBeLessThan(300);
  });

  it('says nothing about a file the caller simply does not have', async () => {
    const { toToolError, VfError, ERROR_CODES } =
      await import('../../../modules/versioned_folders/toolbox/errors.js');
    const log = vi.fn();
    toToolError(new VfError(ERROR_CODES.FILE_NOT_FOUND, 'file not found', { cause: new Error('boom') }), log);
    expect(log).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------- round 4

describe('one file has exactly one name (round 4)', () => {
  // The class: the canonical path the validator RETURNED was thrown away and
  // re-derived by the caller with `split(path.sep)`. On Linux `path.sep` is "/", so
  // every alias of a path — "a//b.txt", "a/./b.txt", "a\\b.txt" — kept its original
  // spelling as a set key, a projected-size key and a response field while
  // containment resolved it to one file. One batch could then claim two outcomes
  // for one path, and an imported tree entry could carry a name `readFile` cannot
  // address.
  it('refuses a batch that names one file by two spellings', async () => {
    const d = await svc.createDraft('site');
    for (const alias of ['a//b.txt', 'a/./b.txt', './a/b.txt']) {
      await expect(svc.applyChanges('site', d.draft_id,
        [{ path: 'a/b.txt', content: '1' }, { path: alias, content: '2' }], [], 'alias'))
        .rejects.toMatchObject({ code: 'INVALID_PATH' });
    }
    // The draft is untouched by any of the refused batches.
    expect((await svc.listFiles('site', { draftId: d.draft_id })).map((f) => f.path))
      .toEqual(['css/style.css', 'index.html']);
  });

  it('refuses a backslash instead of reading it as a separator', async () => {
    const d = await svc.createDraft('site');
    await expect(svc.applyChanges('site', d.draft_id, [{ path: 'a\\b.txt', content: 'x' }], [], 'ms'))
      .rejects.toMatchObject({ code: 'INVALID_PATH', detail: 'paths use forward slashes' });
  });

  it('refuses a write/delete overlap written as an alias', async () => {
    const d = await svc.createDraft('site');
    await expect(svc.applyChanges('site', d.draft_id,
      [{ path: 'css/style.css', content: 'x' }], ['css//style.css'], 'overlap'))
      .rejects.toMatchObject({ code: 'INVALID_PATH' });
  });

  it('answers a canonical response path, and accepts an alias for an existing file', async () => {
    const d = await svc.createDraft('site');
    const r = await svc.applyChanges('site', d.draft_id,
      [{ path: 'a//new.txt', content: 'x' }], ['css/./style.css'], 'canon');
    expect(r.changed).toEqual(['a/new.txt']);          // never the caller's spelling
    expect(r.deleted).toEqual(['css/style.css']);      // and the delete FOUND its file
    expect((await svc.readFile('site', { draftId: d.draft_id }, 'a/new.txt')).content).toBe('x');
  });

  it('imports an aliased source path under a name readFile can address', async () => {
    const svc2 = build({ allowed_folders: 'site,alias' });
    await svc2.init('alias', { files: [{ path: 'a//deep/./f.txt', content: 'ok', encoding: 'utf-8' }] });
    const { current_version } = await svc2.getCurrent('alias');
    const sel = { versionId: current_version };
    expect((await svc2.listFiles('alias', sel)).map((f) => f.path)).toEqual(['a/deep/f.txt']);
    expect((await svc2.readFile('alias', sel, 'a/deep/f.txt')).content).toBe('ok');
  });
});

const nextSecond = () => {
  const s = new Date().getUTCSeconds();
  return new Promise((res) => {
    const t = setInterval(() => { if (new Date().getUTCSeconds() !== s) { clearInterval(t); res(); } }, 20);
  });
};

describe('"newest first" means newest FINALIZED (round 4)', () => {
  // The class: a listing ordered by a Git-derived timestamp instead of by the domain
  // event it claims to order. Version refs are lightweight, so `creatordate` is the
  // date of the commit `applyChanges` made — which can predate finalization by any
  // amount. Finalizing an older draft last therefore listed it second, and
  // `limit: 1` omitted the newest version outright.
  it('lists the last-finalized version first even when its edit is older', async () => {
    const a = await svc.createDraft('site');
    await svc.applyChanges('site', a.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'edit a');
    const b = await svc.createDraft('site');
    await svc.applyChanges('site', b.draft_id, [{ path: 'b.txt', content: 'b' }], [], 'edit b');

    const vb = await svc.finalize('site', b.draft_id, 'b');       // edited second, finalized first
    // Across a second boundary, deliberately: two ids minted in the SAME second tie,
    // and that tie is a documented limit of the id — not the ordering this defends.
    await nextSecond();
    const va = await svc.finalize('site', a.draft_id, 'a');       // edited FIRST, finalized last
    expect(va.version_id > vb.version_id).toBe(true);             // the id encodes finalize time

    const listed = (await svc.listVersions('site')).map((v) => v.version_id);
    expect(listed[0]).toBe(va.version_id);
    expect((await svc.listVersions('site', { limit: 1 })).map((v) => v.version_id)).toEqual([va.version_id]);
  });
});

describe('a sweep that could not enumerate the store is not a completed sweep (round 4)', () => {
  // The class: a failure that is logged and then reported to the caller as a
  // successful empty result. The caller's OK line ("reclaimed 0 draft(s)") then
  // contradicted the ERROR line above it, and that OK is the operator's last word.
  it('fails the startup sweep instead of reporting zero folders', async () => {
    // The enumeration is broken on its own, with the lock sweep left working: a
    // chmod on the store would fail the lock sweep first and prove nothing about
    // this path.
    const be = createBackend();
    const blind = { ...be, listFolders: async () => { throw new Error('store unreadable'); } };
    await expect(build({}, { backend: blind }).startupSweep()).rejects.toThrow(/store unreadable/);

    // And a single damaged FOLDER still does not fail the whole sweep.
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
// bounding or normalization. `apply_changes` puts the agent's change message in
// argv, GitFailure carried argv in its `message`, and toToolError's
// unexpected-error branch interpolated that message raw into toolbox_mcp.log — so
// one newline in a change message wrote a log record of the agent's choosing.
describe('an agent-supplied change message cannot write the operator log', () => {
  const evil = () => `SECRET-SENTINEL${String.fromCharCode(10)}`
    + `[2020-01-01T00:00:00Z] [versioned_folders] OK forged-by-the-agent`
    + String.fromCharCode(0);

  it('keeps every record one bounded line with none of the agent\'s text', async () => {
    const log = vi.fn();
    const s = build({}, { log });
    const d = await s.createDraft('site', 'current', 'x');
    // A NUL in argv makes spawn() throw before git starts, which is the cheapest
    // way to reach the failure path with a message the agent chose.
    let caught;
    try { await s.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], evil()); }
    catch (err) { caught = err; }

    // The backend contract: a GitFailure never leaves the service.
    expect(caught).toBeInstanceOf(VfError);
    expect(caught.code).toBe('GIT_OPERATION_FAILED');

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
    const { runGit } = await import('../../../modules/versioned_folders/toolbox/git-exec.js');
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
    // the single record of the failure would not say what failed.
    const { GitFailure } = await import('../../../modules/versioned_folders/toolbox/errors.js');
    const real = createBackend();
    const raw = { ...real, applyChanges: async () => { throw new GitFailure(128, 'fatal: disk full'); } };
    const s = build({}, { backend: raw });
    const d = await s.createDraft('site', 'current', 'x');
    rows.length = 0;
    const caught = await s.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'm')
      .then(() => null, (e) => e);
    expect(caught).toBeInstanceOf(VfError);                 // never a GitFailure at the boundary
    expect(caught.code).toBe('GIT_OPERATION_FAILED');
    const row = rows.at(-1);
    expect(row).toBeTruthy();
    expect(row.some((v) => v === 'error')).toBe(true);
    expect(row.some((v) => v === 'GIT_OPERATION_FAILED')).toBe(true);
  });

  it('never formats a free-form error message into a log record, anywhere', async () => {
    // The class, asserted STRUCTURALLY. Round 6 bounded these records, which stopped
    // the forgery but not the disclosure: an exception message is written by whoever
    // threw it, and a DB driver quotes the offending value back ("Incorrect string
    // value … for column 'description'" — and that description is agent-supplied).
    // So the rule is now a class and a machine code, never `.message`. Seven sites
    // in five files; git-exec's `error` handler was the one no review named, found by
    // sweeping for the shape rather than by re-reading the finding.
    const dir = new URL('../../../modules/versioned_folders/toolbox/', import.meta.url);
    for (const file of ['errors.js', 'audit.js', 'service.js', 'versioned-folders.js', 'git-exec.js']) {
      const src = await readFile(new URL(file, dir), 'utf8');
      src.split('\n').forEach((line, i) => {
        if (line.trimStart().startsWith('*') || line.trimStart().startsWith('//')) return;
        expect(line, `${file}:${i + 1} formats a raw error message`)
          .not.toMatch(/(err|err2|e)\??\.message/);
      });
    }
  });

  it('discloses no caller text when GIT quotes the path back', async () => {
    // The reviewer's reproduction, and the one that disproved the argument for keeping
    // git's stderr: git ECHOES THE PATHSPEC, so a legal path (only NUL and ".." are
    // refused) puts caller text in stderr without any store damage at all.
    const log = vi.fn();
    const s = build({}, { log });
    const d = await s.createDraft('site', 'current', 'x');
    const dir = `SECRET-SENTINEL${String.fromCharCode(10)}[forged]`;
    await s.applyChanges('site', d.draft_id, [{ path: `${dir}/file.txt`, content: 'x' }], [], 'add');
    log.mockClear();
    // Read the DIRECTORY as if it were a file: git answers "…: bad file" and quotes it.
    const err = await s.readFile('site', { draftId: d.draft_id }, dir).then(() => null, (e) => e);
    expect(err).toBeTruthy();
    const resp = toToolError(err, log);
    expect(JSON.stringify(resp)).not.toMatch(/SECRET-SENTINEL/);
    const records = log.mock.calls.map(([, , m]) => String(m));
    expect(records.join('|')).not.toMatch(/SECRET-SENTINEL/);
    expect(records.join('|')).not.toMatch(/forged/);
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

// The class: a contract that names one source accepting zero or two. Both read
// tools take draft_id and version_id as independently optional arguments; naming
// both selected the draft and ignored the version SILENTLY, which is worse than a
// refusal because the caller reads a file it did not ask for.
describe('a read names exactly one source', () => {
  it('refuses both and neither, on both read tools, before the store is touched', async () => {
    const d = await svc.createDraft('site', 'current', 'x');
    await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'add');
    const v = await svc.finalize('site', d.draft_id, 'v');

    for (const selector of [{ draftId: d.draft_id, versionId: v.version_id }, {}]) {
      await expect(svc.listFiles('site', selector)).rejects.toThrow(/INVALID_PATH/);
      await expect(svc.readFile('site', selector, 'index.html')).rejects.toThrow(/INVALID_PATH/);
    }
    // "Before the store is touched": the same malformed selector is refused for a
    // folder that does not exist, rather than reporting FOLDER_NOT_FOUND first.
    const wide = build({ allowed_folders: 'site,ghost' });
    await expect(wide.listFiles('ghost', {})).rejects.toThrow(/INVALID_PATH/);
    await expect(wide.listFiles('ghost', { draftId: d.draft_id, versionId: v.version_id }))
      .rejects.toThrow(/INVALID_PATH/);
  });

  it('still reads with exactly one of them', async () => {
    const names = (files) => files.map((f) => f.path);
    const d = await svc.createDraft('site', 'current', 'x');
    expect(names(await svc.listFiles('site', { draftId: d.draft_id }))).toContain('index.html');
    await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'a' }], [], 'add');
    const v = await svc.finalize('site', d.draft_id, 'v');
    expect(names(await svc.listFiles('site', { versionId: v.version_id }))).toContain('index.html');
    expect((await svc.readFile('site', { versionId: v.version_id }, 'index.html')).content)
      .toContain('v1');
  });
});
