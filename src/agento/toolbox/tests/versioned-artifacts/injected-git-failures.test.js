// Two guards, in their own file because they need a module mock. Both are about a
// git command that fails for a reason no on-disk damage provokes reliably, so the
// failure is injected at the exec boundary and every other git call stays real.
//
// 1. The staged-changes probe must accept ONLY `git diff --quiet`'s documented
//    "differences found" exit (1) as a difference. Any other nonzero exit is a
//    failed command, and reading it as "there are changes" would commit a batch
//    on the strength of an error.
// 2. A discard's marker deletion is the last step of a resumable sequence, so a
//    failure there must be REPORTED. Reporting success while the marker stands
//    leaves a ref nothing will ever clean up, and the caller believes the draft is
//    closed; the next call must be able to finish the same sequence.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { GitFailure } from '../../../modules/versioned_artifacts/toolbox/errors.js';

const state = { failStagedProbe: false, failMarkerDeletes: 0, markerDeletesFailed: 0 };

vi.mock('../../../modules/versioned_artifacts/toolbox/git-exec.js', async (importOriginal) => {
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
          && argv.some((a) => typeof a === 'string' && a.includes('/discarding/'))) {
        state.failMarkerDeletes -= 1;
        state.markerDeletesFailed += 1;
        throw new GitFailure(128, 'fatal: cannot lock ref');
      }
      return real.runGit(argv, opts);
    },
  };
});

const { createService } = await import('../../../modules/versioned_artifacts/toolbox/service.js');
const { createBackend } = await import('../../../modules/versioned_artifacts/toolbox/git-backend.js');
const { readSource } = await import('./helpers.js');

let root, pub, src, svc, be;

beforeEach(async () => {
  state.failStagedProbe = false;
  state.failMarkerDeletes = 0;
  state.markerDeletesFailed = 0;
  be = createBackend();
  root = await mkdtemp(path.join(tmpdir(), 'vf-staged-'));
  pub = await mkdtemp(path.join(tmpdir(), 'vf-staged-pub-'));
  src = await mkdtemp(path.join(tmpdir(), 'vf-stagedsrc-'));
  await mkdir(path.join(src, 'css'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  svc = createService({
    config: { storage_root: root, published_root: pub, allowed_artifacts: 'site',
      'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080', 'limits/max_files': 2000,
      'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600,
      'limits/max_diff_bytes': 1048576, 'limits/max_agent_artifacts': 50, 'security/allow_symlinks': false },
    db: null, log: vi.fn(), actor: 'admin',
  });
  await svc.init('site', { files: await readSource(src) });
});
afterEach(async () => { await rm(root, {recursive:true,force:true}); await rm(pub, {recursive:true,force:true}); await rm(src, {recursive:true,force:true}); });

describe('the staged-changes probe trusts only the documented exit code', () => {
  // Driven through the backend, because the probe is `commitDraft`'s and the service
  // call that reaches it now needs a desk — a Linux-only fixture this guard does not
  // need to depend on.
  const edit = async (draftId, body) =>
    writeFile(path.join(be.getDraftPath(root, 'site', draftId), 'a.txt'), body);

  it('reports a failed probe as a storage failure, not as "there are changes"', async () => {
    const d = await svc.createDraft('site');
    await edit(d.draft_id, 'x');
    state.failStagedProbe = true;
    await expect(be.commitDraft(root, 'site', d.draft_id, 'add'))
      .rejects.toMatchObject({ code: 'STORAGE_OPERATION_FAILED' });
    state.failStagedProbe = false;
    // The draft is still usable: nothing was committed on the strength of the error.
    const r = await be.commitDraft(root, 'site', d.draft_id, 'add');
    expect(r.committed).toBe(true);
  });

  it('still reports "nothing committed" when the worktree matches the tip (exit 0 keeps its meaning)', async () => {
    const d = await svc.createDraft('site');
    const r = await be.commitDraft(root, 'site', d.draft_id, 'noop');
    expect(r.committed).toBe(false);
  });
});

describe('a discard reports a failed marker deletion instead of swallowing it', () => {
  it('fails the discard, keeps the marker, and converges on the next call', async () => {
    const d = await svc.createDraft('site');
    state.failMarkerDeletes = 1;

    await expect(svc.discardDraft('site', d.draft_id))
      .rejects.toMatchObject({ code: 'STORAGE_OPERATION_FAILED' });

    expect(state.markerDeletesFailed).toBe(1);        // the injection really fired
    // The marker is what makes the retry resumable: the draft is in `discarding`, and
    // the next call finishes the same sequence rather than starting a new one.
    expect(await svc.discardDraft('site', d.draft_id)).toEqual({ draft_id: d.draft_id, discarded: true });
    await expect(svc.discardDraft('site', d.draft_id)).rejects.toMatchObject({ code: 'DRAFT_NOT_FOUND' });
  });
});
