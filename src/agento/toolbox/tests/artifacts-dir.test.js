import { describe, it, expect, vi } from 'vitest';
import {
  buildArtifactsDir, buildDownloadsDir, FALLBACK_ARTIFACTS_DIR, loadDownloadsDir,
} from '../artifacts-dir.js';

const META = { workspaceCode: 'acme', agentViewCode: 'dev' };

describe('buildArtifactsDir', () => {
  it.each([
    [{ workspaceCode: '_threads', agentViewCode: 'av' }],
    [{ workspaceCode: 'ws', agentViewCode: '_fallback' }],
    [{ workspaceCode: '-ws', agentViewCode: 'av' }],
    [{ workspaceCode: 'a.b', agentViewCode: 'av' }],
    [{ workspaceCode: 'ws', agentViewCode: 'a/b' }],
  ])('a code that may not name a run dir gets the fallback (%j)', (meta) => {
    expect(buildArtifactsDir(meta, 7, null)).toBe(FALLBACK_ARTIFACTS_DIR);
  });

  it('scopes a job by its numeric id', () => {
    expect(buildArtifactsDir(META, 42, null)).toBe('/workspace/artifacts/acme/dev/42');
  });

  it('scopes a job-less run by its run id', () => {
    // The regression: an interactive `agento run` names no job, and every such session
    // shared `_fallback` — so the versioned-artifacts desk tools refused all of them.
    expect(buildArtifactsDir(META, null, 'cli-abc123'))
      .toBe('/workspace/artifacts/acme/dev/cli-abc123');
  });

  it('prefers the job id when both are named', () => {
    expect(buildArtifactsDir(META, 42, 'cli-abc123')).toBe('/workspace/artifacts/acme/dev/42');
  });

  it('falls back when the session names neither', () => {
    expect(buildArtifactsDir(META, null, null)).toBe(FALLBACK_ARTIFACTS_DIR);
  });

  it('falls back when the agent_view is unknown', () => {
    expect(buildArtifactsDir(null, null, 'cli-abc123')).toBe(FALLBACK_ARTIFACTS_DIR);
  });

  // The run id is the LAST PATH SEGMENT, asserted by the caller. Anything that is not one
  // plain segment, and `_fallback` itself, must land on the fallback rather than build a
  // path — `desk-io.js` then refuses it, which is the honest answer.
  it.each([
    ['_fallback', '_fallback'],
    ['a traversal', '../../etc'],
    ['a dot', 'run.1'],
    ['a separator', 'acme/dev/9'],
    ['an empty string', ''],
    ['an over-long id', 'x'.repeat(65)],
    ['a repeated query param', ['a', 'b'].toString()],
  ])('refuses %s as a run id', (_name, runId) => {
    expect(buildArtifactsDir(META, null, runId)).toBe(FALLBACK_ARTIFACTS_DIR);
  });
});

describe('thread downloads dir', () => {
  const KEY = '0123456789abcdef0123456789abcdef';

  it('names the thread key dir', () => {
    expect(buildDownloadsDir(KEY)).toBe(`/workspace/artifacts/_threads/${KEY}/downloads`);
  });

  it.each([null, undefined, '', '../x', KEY.toUpperCase(), `${KEY}0`, 42])('refuses %j as a key', (key) => {
    expect(buildDownloadsDir(key)).toBeNull();
  });

  it('reads the key of the capability job thread', async () => {
    const query = vi.fn(async () => [[{ key: KEY }]]);
    expect(await loadDownloadsDir(query, 7)).toBe(`/workspace/artifacts/_threads/${KEY}/downloads`);
    expect(query.mock.calls[0][1]).toEqual([7]);
  });

  it('is null with no job and with no thread', async () => {
    const query = vi.fn(async () => [[]]);
    expect(await loadDownloadsDir(query, null)).toBeNull();
    expect(query).not.toHaveBeenCalled();
    expect(await loadDownloadsDir(query, 7)).toBeNull();
  });
});
