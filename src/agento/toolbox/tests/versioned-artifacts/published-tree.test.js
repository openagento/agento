import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, mkdir, writeFile, readFile, readlink, realpath, readdir } from 'node:fs/promises';
import fs from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createService } from '../../../modules/versioned_artifacts/toolbox/service.js';
import { createBackend } from '../../../modules/versioned_artifacts/toolbox/git-backend.js';
import { pruneVersions, swapCurrent } from '../../../modules/versioned_artifacts/toolbox/published-tree.js';
import { mintVersion, readSource } from './helpers.js';

// `saveVersion` takes a desk, which is fd-anchored and Linux-only. The published tree
// itself is plain fs, so everything reached through `publish` runs everywhere.
const linux = process.platform === 'linux';

let root, pub, src, svc, be, desk, deskFd;
const opened = [];
const cfg = (over = {}) => ({ storage_root: root, published_root: pub, allowed_artifacts: 'site',
  'limits/max_files': 2000, 'limits/max_file_size': 5242880, 'limits/max_total_size': 104857600,
  'limits/max_diff_bytes': 1048576, 'limits/max_agent_artifacts': 50, 'security/allow_symlinks': false,
  'serving/keep_versions': 0, 'serving/public_base_url': 'http://localhost:8080', ...over });

const mk = (over = {}, backend = undefined) => createService({
  config: cfg(over), db: null, log: vi.fn(), actor: 'a@b.c', ...(backend ? { backend } : {}),
});

beforeEach(async () => {
  be = createBackend();
  root = await mkdtemp(path.join(tmpdir(), 'va-store-'));
  pub = await mkdtemp(path.join(tmpdir(), 'va-pub-'));
  src = await mkdtemp(path.join(tmpdir(), 'va-src-'));
  await writeFile(path.join(src, 'index.html'), '<h1>v1</h1>\n');
  await mkdir(path.join(src, 'css'), { recursive: true });
  await writeFile(path.join(src, 'css/style.css'), 'body{}\n');
  svc = mk();
  await svc.init('site', { files: await readSource(src) });
  desk = await mkdtemp(path.join(tmpdir(), 'va-desk-'));
  if (linux) { deskFd = fs.openSync(desk, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY); opened.push(deskFd); }
});
afterEach(async () => {
  while (opened.length) { try { fs.closeSync(opened.pop()); } catch { /* already closed */ } }
  for (const d of [root, pub, src, desk]) await rm(d, { recursive: true, force: true });
});

const mkVersion = (content) => mintVersion(be, root, 'site', content);

const vDir = (id) => path.join(pub, 'site', 'v', id);
const currentLink = () => path.join(pub, 'site', 'current');

describe('published tree', () => {
  it('publishes version 1 at init, so the preview url resolves before any save', async () => {
    // `init` answers a preview URL. A URL handed to a human that resolves to nothing is
    // the gap this closes: the store's current and the served current agree from birth.
    const v1 = (await svc.getCurrent('site')).current_version;
    expect(await readFile(path.join(vDir(v1), 'index.html'), 'utf8')).toBe('<h1>v1</h1>\n');
    expect(await readlink(currentLink())).toBe(path.join('v', v1));
  });

  it('materializes the target bytes under <root>/<code>/v/<id>/', async () => {
    const v1 = (await svc.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    await svc.publish('site', v2, v1);
    expect(await readFile(path.join(vDir(v2), 'index.html'), 'utf8')).toBe('<h1>v2</h1>\n');
    expect(await readFile(path.join(vDir(v2), 'css/style.css'), 'utf8')).toBe('body{}\n');
  });

  it('points current at a RELATIVE target whose realpath stays inside the root', async () => {
    const v1 = (await svc.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    await svc.publish('site', v2, v1);
    // Relative, so the link is correct from inside the serving container too, whatever
    // absolute path the published root is mounted at there.
    expect(await readlink(currentLink())).toBe(path.join('v', v2));
    const real = await realpath(currentLink());
    expect(real.startsWith(await realpath(pub))).toBe(true);
    expect(await readFile(path.join(currentLink(), 'index.html'), 'utf8')).toBe('<h1>v2</h1>\n');
  });

  it('leaves no current.tmp-* behind and moves current off the old version', async () => {
    // The direct regression for the `mv` swap, which left `current` on the old version
    // and dropped a stray link INSIDE it.
    const v1 = (await svc.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    await svc.publish('site', v2, v1);
    const entries = await readdir(path.join(pub, 'site'));
    expect(entries.filter((e) => e.startsWith('current.tmp-'))).toEqual([]);
    expect(await readdir(path.join(currentLink()))).not.toContain('current');
  });

  it('prunes nothing when keep_versions is 0', async () => {
    const s = mk({ 'serving/keep_versions': 0 });
    let prev = (await s.getCurrent('site')).current_version;
    const ids = [prev];
    for (let i = 0; i < 3; i += 1) {
      const v = await mkVersion(`<h1>v${i + 2}</h1>\n`);
      await s.publish('site', v, prev);
      prev = v; ids.push(v);
    }
    for (const id of ids.slice(1)) expect(fs.existsSync(vDir(id))).toBe(true);
  });

  it('keeps the newest N and the current target even when current is older', async () => {
    const s = mk({ 'serving/keep_versions': 2 });
    const v1 = (await s.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    const v3 = await mkVersion('<h1>v3</h1>\n');
    const v4 = await mkVersion('<h1>v4</h1>\n');
    await s.publish('site', v2, v1);
    await s.publish('site', v3, v2);
    await s.publish('site', v4, v3);
    // Retention really prunes: v2 is now the third-newest and is not the current target.
    expect(fs.existsSync(vDir(v2))).toBe(false);
    // Publishing the OLD v2 again makes `current` older than the two newest directories.
    await s.publish('site', v2, v4);
    expect(fs.existsSync(vDir(v2))).toBe(true);      // survives only as the current target
    expect(fs.existsSync(vDir(v3))).toBe(true);
    expect(fs.existsSync(vDir(v4))).toBe(true);
    expect(await readlink(currentLink())).toBe(path.join('v', v2));
  });

  it('deletes nothing when it cannot read what current points at', async () => {
    // `readlink` answers EINVAL when `current` is a real directory — an errno, not an
    // absence. Read as "no current", it makes retention delete the directory the server
    // is serving right now and leaves the link dangling.
    const v1 = (await svc.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    await svc.publish('site', v2, v1);
    await rm(currentLink(), { force: true });
    await mkdir(currentLink(), { recursive: true });
    await expect(pruneVersions(pub, 'site', 1)).rejects.toMatchObject({ code: 'EINVAL' });
    expect((await readdir(path.join(pub, 'site', 'v'))).sort()).toEqual([v1, v2].sort());
  });

  it('reports preview_path null for a pruned version that is still materializable', async () => {
    const s = mk({ 'serving/keep_versions': 1 });
    const v1 = (await s.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    await s.publish('site', v2, v1);
    const v3 = await mkVersion('<h1>v3</h1>\n');
    await s.publish('site', v3, v2);
    expect(fs.existsSync(vDir(v2))).toBe(false);
    const rows = await s.listVersions('site');
    expect(rows.find((r) => r.version_id === v2).preview_path).toBeNull();
    expect(rows.find((r) => r.version_id === v3).preview_path).toBe(`/site/v/${v3}/`);
    // Pruning removes the PREVIEW, never the version.
    if (linux) {
      await s.materialize('site', { versionId: v2 }, deskFd);
      expect(await readFile(path.join(desk, 'index.html'), 'utf8')).toBe('<h1>v2</h1>\n');
    }
  });

  it('re-materializes a pruned version when it is published again', async () => {
    const s = mk({ 'serving/keep_versions': 1 });
    const v1 = (await s.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    await s.publish('site', v2, v1);
    const v3 = await mkVersion('<h1>v3</h1>\n');
    await s.publish('site', v3, v2);
    expect(fs.existsSync(vDir(v2))).toBe(false);
    await s.publish('site', v2, v3);
    expect(await readFile(path.join(vDir(v2), 'index.html'), 'utf8')).toBe('<h1>v2</h1>\n');
    expect(await readlink(currentLink())).toBe(path.join('v', v2));
  });

  it('exposes the absolute preview url on publish and get_current, never on a version row', async () => {
    const v1 = (await svc.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    expect((await svc.publish('site', v2, v1)).preview_url).toBe('http://localhost:8080/site/');
    expect((await svc.getCurrent('site')).preview_url).toBe('http://localhost:8080/site/');
    // A wrong absolute URL in a message to a human is worse than no URL, so the
    // listing stays relative.
    for (const row of await svc.listVersions('site')) expect(row.preview_url).toBeUndefined();
  });
});

describe('publish ordering and recovery', () => {
  // `init` swaps AFTER its own lock releases, so a publish can complete in between. The
  // swap then reads the store's current and installs version 1 only if current is still
  // it — otherwise it leaves the fresher publish standing. Reproduced by moving BOTH the
  // store ref and the served pointer to v2 from inside the backend's `init`, the same
  // window; the racing publish is done with backend primitives so it does not re-enter
  // the one lifecycle lock init already holds.
  it('does not drag the served current back to version 1 when a publish lands during init', async () => {
    const over = { allowed_artifacts: 'site2' };
    let v2;
    const raced = {
      ...be,
      init: async (...args) => {
        const made = await be.init(...args);
        v2 = await mintVersion(be, root, 'site2', '<h1>v2</h1>');
        await be.materializePublished(root, 'site2', v2, {
          destDir: path.join(pub, 'site2', 'v', v2), scratchDir: path.join(pub, '.tmp'),
        });
        await be.publish(root, 'site2', v2, made.current_version);   // moves the store ref
        await swapCurrent(pub, 'site2', v2);                          // moves the served pointer
        return made;
      },
    };
    await mk(over, raced).init('site2', { files: await readSource(src) });

    expect((await be.getCurrent(root, 'site2')).current_version).toBe(v2);
    expect(await readlink(path.join(pub, 'site2', 'current'))).toBe(path.join('v', v2));
  });

  // The whole point of materialize-then-CAS-then-swap: a failure in step 1 must leave
  // the STORE untouched, so the caller retries with the arguments it already has.
  it('leaves the store current unmoved when materialization fails, and succeeds on retry', async () => {
    const real = createBackend();
    let fail = true;
    const stub = { ...real, materializePublished: async (...a) => {
      if (fail) throw new Error('disk full');
      return real.materializePublished(...a);
    } };
    const s = mk({}, stub);
    const v1 = (await s.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    await expect(s.publish('site', v2, v1)).rejects.toThrow();
    expect((await s.getCurrent('site')).current_version).toBe(v1);
    fail = false;
    await s.publish('site', v2, v1);
    expect((await s.getCurrent('site')).current_version).toBe(v2);
  });

  it('returns preview_stale instead of throwing when the swap fails, and converges on repair', async () => {
    const v1 = (await svc.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    // A real DIRECTORY at `current` makes rename() fail EISDIR — uid-independent, so it
    // fails for root in CI exactly as it does for a normal user. The link `init`
    // installed goes first, or `mkdir -p` would follow it and succeed silently.
    await rm(currentLink(), { force: true });
    await mkdir(currentLink(), { recursive: true });
    const r = await svc.publish('site', v2, v1);
    expect(r.preview_stale).toBe(true);
    expect(r.current_version).toBe(v2);
    // The store moved; the served tree did not. The repair is the ORDINARY call.
    await rm(currentLink(), { recursive: true, force: true });
    const repaired = await svc.publish('site', v2, v2);
    expect(repaired.preview_stale).toBeUndefined();
    expect(await readlink(currentLink())).toBe(path.join('v', v2));
    expect((await svc.getCurrent('site')).current_version).toBe(v2);
  });

  it('refuses the repair call when current sits on a different version', async () => {
    // Proof the repair path did NOT become a CAS bypass.
    const v1 = (await svc.getCurrent('site')).current_version;
    const v2 = await mkVersion('<h1>v2</h1>\n');
    await expect(svc.publish('site', v2, v2)).rejects.toMatchObject({ code: 'CURRENT_VERSION_CHANGED' });
    expect((await svc.getCurrent('site')).current_version).toBe(v1);
  });
});

describe('save version', () => {
  it.runIf(linux)('materializes the saved version and answers its preview path', async () => {
    const d = await svc.createDraft('site', 'current', 'edit');
    await svc.materialize('site', { draftId: d.draft_id }, deskFd);
    await writeFile(path.join(desk, 'index.html'), '<h1>saved</h1>\n');
    const saved = await svc.saveVersion('site', d.draft_id, deskFd, 'msg');
    expect(saved.preview_path).toBe(`/site/v/${saved.version_id}/`);
    expect(await readFile(path.join(vDir(saved.version_id), 'index.html'), 'utf8')).toBe('<h1>saved</h1>\n');
  });

  it.runIf(linux)('never fails a completed save because the published tree could not be written', async () => {
    const real = createBackend();
    const stub = { ...real, materializePublished: async () => { throw new Error('disk full'); } };
    const log = vi.fn();
    const s = createService({ config: cfg(), db: null, log, actor: 'a@b.c', backend: stub });
    const d = await s.createDraft('site', 'current', 'edit');
    await s.materialize('site', { draftId: d.draft_id }, deskFd);
    await writeFile(path.join(desk, 'index.html'), '<h1>saved</h1>\n');
    const saved = await s.saveVersion('site', d.draft_id, deskFd, 'msg');
    expect(saved.version_id).toBeTruthy();
    expect(saved.preview_path).toBeNull();
    expect(log).toHaveBeenCalled();
  });
});
