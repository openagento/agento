// Two guards, in their own file because they need a module mock. Both are about a
// git command that fails for a reason no on-disk damage provokes reliably, so the
// failure is injected at the exec boundary and every other git call stays real.
//
// 1. The staged-changes probe must accept ONLY `git diff --quiet`'s documented
//    "differences found" exit (1) as a difference. Any other nonzero exit is a
//    failed command, and reading it as "there are changes" would commit a batch
//    on the strength of an error.
// 2. Finalize's completion-marker deletion is the last step of a resumable
//    sequence, so its failure must carry FINALIZE_FAILED — the only code the
//    service retries. With GIT_OPERATION_FAILED the retry is silently disabled
//    and the marker survives, which makes a later finalize skip steps 1-3.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { GitFailure } from '../../../modules/versioned_folders/toolbox/errors.js';

const state = { failStagedProbe: false, failMarkerDeletes: 0, markerDeletesFailed: 0 };

vi.mock('../../../modules/versioned_folders/toolbox/git-exec.js', async (importOriginal) => {
  const real = await importOriginal();
  return {
    ...real,
    runGit: (argv, opts) => {
      if (state.failStagedProbe && argv.includes('--cached') && argv.includes('--quiet')) {
        throw new GitFailure(2, 'fatal: unable to read index');
      }
      // Thrown BEFORE the real call, so the marker ref survives — which is what
      // makes deleteRefVerified's own existence check confirm a real failure.
      if (state.failMarkerDeletes > 0 && argv.includes('-d')
          && argv.some((a) => typeof a === 'string' && a.includes('/finalized/'))) {
        state.failMarkerDeletes -= 1;
        state.markerDeletesFailed += 1;
        throw new GitFailure(128, 'fatal: cannot lock ref');
      }
      return real.runGit(argv, opts);
    },
  };
});

const { createService } = await import('../../../modules/versioned_folders/toolbox/service.js');
const { readSource } = await import('./helpers.js');

let root, src, svc;

beforeEach(async () => {
  state.failStagedProbe = false;
  state.failMarkerDeletes = 0;
  state.markerDeletesFailed = 0;
  root = await mkdtemp(path.join(tmpdir(), 'vf-staged-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-stagedsrc-'));
  await mkdir(path.join(src, 'css'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  svc = createService({
    config: { storage_root: root, allowed_folders: 'site', 'limits/max_files': 2000,
      'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600,
      'limits/max_diff_bytes': 1048576, 'security/allow_symlinks': false },
    db: null, log: vi.fn(), actor: 'admin',
  });
  await svc.init('site', { files: await readSource(src) });
});
afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(src, {recursive:true,force:true}); });

describe('the staged-changes probe trusts only the documented exit code', () => {
  it('reports a failed probe as a storage failure, not as "there are changes"', async () => {
    const d = await svc.createDraft('site');
    state.failStagedProbe = true;
    await expect(svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'x' }], [], 'add'))
      .rejects.toMatchObject({ code: 'GIT_OPERATION_FAILED' });
    state.failStagedProbe = false;
    // The draft is still usable: nothing was committed on the strength of the error.
    const r = await svc.applyChanges('site', d.draft_id, [{ path: 'a.txt', content: 'x' }], [], 'add');
    expect(r.changed).toEqual(['a.txt']);
  });

  it('still refuses a batch that changes nothing (exit 0 keeps its meaning)', async () => {
    const d = await svc.createDraft('site');
    await expect(svc.applyChanges('site', d.draft_id, [{ path: 'index.html', content: '<h1>v1</h1>\n' }], [], 'noop'))
      .rejects.toMatchObject({ code: 'DRAFT_HAS_NO_CHANGES' });
  });
});

describe('finalize retries a failed marker deletion, and only that', () => {
  const change = (svc, id) =>
    svc.applyChanges('site', id, [{ path: 'index.html', content: '<h1>v2</h1>\n' }], [], 'edit');

  it('converges when the first marker deletion fails', async () => {
    const before = (await svc.listVersions('site')).map((x) => x.version_id);
    const d = await svc.createDraft('site');
    await change(svc, d.draft_id);
    state.failMarkerDeletes = 1;
    const v = await svc.finalize('site', d.draft_id, 'v2');
    expect(state.markerDeletesFailed).toBe(1);        // the injection really fired
    expect(v.version_id).toMatch(/^v-\d{8}-\d{6}-[a-z0-9]{4}$/);
    // Exactly ONE new version: the retry finished the teardown, it did not mint
    // a second version for the same content. Asserted as a SET: versions are sorted by
    // their id, whose timestamp has one-second resolution, so two versions minted inside
    // the same second tie and their relative order is not this test's subject.
    const after = (await svc.listVersions('site')).map((x) => x.version_id);
    expect([...after].sort()).toEqual([v.version_id, ...before].sort());
    await expect(svc.listFiles('site', { draftId: d.draft_id }))
      .rejects.toMatchObject({ code: 'DRAFT_NOT_FOUND' });
  });

  it('surfaces FINALIZE_FAILED, not a generic storage failure, when it keeps failing', async () => {
    const d = await svc.createDraft('site');
    await change(svc, d.draft_id);
    state.failMarkerDeletes = 99;
    await expect(svc.finalize('site', d.draft_id, 'v2'))
      .rejects.toMatchObject({ code: 'FINALIZE_FAILED' });
    expect(state.markerDeletesFailed).toBe(2);        // the one retry ran, and stopped there
  });
});
