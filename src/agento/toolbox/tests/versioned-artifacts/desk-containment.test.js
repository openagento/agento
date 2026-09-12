import { describe, it, expect, afterEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';

// Imported at MODULE level on purpose: `desk-io.js` is Linux-only, and this import
// proves the refusal is at CALL time. An import-time throw would take every macOS
// dev host's `tool-declaration.test.js` red, because that suite executes every
// module's `register()` and reaches this file transitively.
import {
  ARTIFACTS_ROOT, openDesk, closeDesk, readdirAt, emptyDesk, mirrorIn, mirrorOut,
} from '../../../modules/versioned_artifacts/toolbox/desk-io.js';
import { ERROR_CODES } from '../../../modules/versioned_artifacts/toolbox/errors.js';

const linux = process.platform === 'linux';
const opened = [];
const keep = (fd) => { opened.push(fd); return fd; };
const sessions = [];
afterEach(() => {
  while (opened.length) { try { fs.closeSync(opened.pop()); } catch { /* already closed */ } }
  // The sessions live under the REAL mount point, not a temp dir nothing else shares, so
  // the suite removes its own — otherwise a developer's /workspace fills up run by run.
  while (sessions.length) fs.rmSync(sessions.pop(), { recursive: true, force: true });
});

let seq = 0;
// A unique <ws>/<av>/<job> under the REAL constant, so the suite exercises the
// production root rather than a parameter injected for testability.
function newSession() {
  const id = `${process.pid}${seq++}`;
  const ws = path.join(ARTIFACTS_ROOT, `ws${id}`);
  const dir = path.join(ws, `av${id}`, `${id}`);
  fs.mkdirSync(dir, { recursive: true });
  sessions.push(ws);
  return dir;
}
const deskPath = (artifactsDir, code, id) => path.join(artifactsDir, 'versioned-artifacts', code, id);

it('imports on every platform and refuses only when called', () => {
  expect(typeof openDesk).toBe('function');
  const saved = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: 'darwin', configurable: true });
  try {
    expect(() => openDesk('/workspace/artifacts/ws/av/1', 'site', 'd-abcdef', { create: false }))
      .toThrow(/STORAGE_OPERATION_FAILED/);
  } finally { Object.defineProperty(process, 'platform', saved); }
});

describe.skipIf(!linux)('the walk is the containment proof', () => {
  it('opens an existing desk and reads it through the descriptor', () => {
    const ad = newSession();
    fs.mkdirSync(deskPath(ad, 'site', 'd-abcdef'), { recursive: true });
    fs.writeFileSync(path.join(deskPath(ad, 'site', 'd-abcdef'), 'a.txt'), 'hi');

    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: false }));
    expect(readdirAt(fd).map((e) => e.name)).toEqual(['a.txt']);
  });

  it('refuses a missing desk with DESK_MISSING when create is false', () => {
    const ad = newSession();
    expect(() => openDesk(ad, 'site', 'd-abcdef', { create: false }))
      .toThrow(new RegExp(ERROR_CODES.DESK_MISSING));
  });

  it('creates the whole chain when create is true', () => {
    const ad = newSession();
    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: true }));
    expect(readdirAt(fd)).toEqual([]);
    expect(fs.statSync(deskPath(ad, 'site', 'd-abcdef')).isDirectory()).toBe(true);
  });

  it('is idempotent: create over an existing desk keeps its contents', () => {
    const ad = newSession();
    const desk = deskPath(ad, 'site', 'd-abcdef');
    fs.mkdirSync(desk, { recursive: true });
    fs.writeFileSync(path.join(desk, 'keep.txt'), 'x');

    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: true }));
    expect(readdirAt(fd).map((e) => e.name)).toEqual(['keep.txt']);
  });

  it.each([
    ['a _fallback session', `${ARTIFACTS_ROOT}/_fallback`],
    ['a directory outside the mount point', '/tmp/not-the-artifacts-root'],
    ['the mount point itself', ARTIFACTS_ROOT],
  ])('refuses %s with WORKSPACE_UNAVAILABLE', (_name, ad) => {
    expect(() => openDesk(ad, 'site', 'd-abcdef', { create: true }))
      .toThrow(new RegExp(ERROR_CODES.WORKSPACE_UNAVAILABLE));
  });

  it('refuses a symlinked component encountered during the walk', () => {
    const ad = newSession();
    const parent = path.dirname(ad);
    fs.rmdirSync(ad);
    fs.symlinkSync('/etc', ad);

    expect(() => openDesk(ad, 'site', 'd-abcdef', { create: true }))
      .toThrow(new RegExp(ERROR_CODES.SYMLINK_NOT_ALLOWED));
    // The link itself was not followed, so /etc is untouched and still populated.
    expect(fs.readdirSync(parent)).toContain(path.basename(ad));
  });

  it('refuses a symlink planted as the desk itself', () => {
    const ad = newSession();
    const chain = path.join(ad, 'versioned-artifacts', 'site');
    fs.mkdirSync(chain, { recursive: true });
    fs.symlinkSync('/etc', path.join(chain, 'd-abcdef'));

    expect(() => openDesk(ad, 'site', 'd-abcdef', { create: false }))
      .toThrow(new RegExp(`${ERROR_CODES.SYMLINK_NOT_ALLOWED}|${ERROR_CODES.DESK_MISSING}`));
  });

  // The case the walk exists for: replacement AFTER validation, not only before it.
  it('keeps reading the real desk after a component is swapped for a symlink', () => {
    const ad = newSession();
    const desk = deskPath(ad, 'site', 'd-abcdef');
    fs.mkdirSync(desk, { recursive: true });
    fs.writeFileSync(path.join(desk, 'real.txt'), 'mine');

    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: false }));

    fs.renameSync(ad, `${ad}-moved`);
    fs.symlinkSync('/etc', ad);

    expect(readdirAt(fd).map((e) => e.name)).toEqual(['real.txt']);
    expect(() => openDesk(ad, 'site', 'd-abcdef', { create: false }))
      .toThrow(new RegExp(`${ERROR_CODES.SYMLINK_NOT_ALLOWED}|${ERROR_CODES.DESK_MISSING}`));
  });

  it('rejects an identifier that is not a single safe component', () => {
    const ad = newSession();
    for (const bad of ['..', 'a/b', '.git', '']) {
      expect(() => openDesk(ad, 'site', bad, { create: true })).toThrow(/INVALID_PATH/);
    }
  });

  it('leaks no descriptor across many opens', () => {
    const ad = newSession();
    const before = fs.readdirSync('/proc/self/fd').length;
    for (let i = 0; i < 40; i++) closeDesk(openDesk(ad, 'site', 'd-abcdef', { create: true }));
    expect(fs.readdirSync('/proc/self/fd').length).toBeLessThanOrEqual(before + 2);
  });
});

describe.skipIf(!linux)('emptyDesk clears through the descriptor', () => {
  it('removes files and nested directories', () => {
    const ad = newSession();
    const desk = deskPath(ad, 'site', 'd-abcdef');
    fs.mkdirSync(path.join(desk, 'sub', 'deep'), { recursive: true });
    fs.writeFileSync(path.join(desk, 'top.txt'), 'a');
    fs.writeFileSync(path.join(desk, 'sub', 'deep', 'b.txt'), 'b');

    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: false }));
    emptyDesk(fd);
    expect(readdirAt(fd)).toEqual([]);
    expect(fs.existsSync(desk)).toBe(true);
  });

  it('unlinks a planted symlink instead of clearing its target', () => {
    const ad = newSession();
    const desk = deskPath(ad, 'site', 'd-abcdef');
    fs.mkdirSync(desk, { recursive: true });
    const victim = path.join(ad, 'victim');
    fs.mkdirSync(victim);
    fs.writeFileSync(path.join(victim, 'survivor.txt'), 'alive');
    fs.symlinkSync(victim, path.join(desk, 'link'));

    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: false }));
    emptyDesk(fd);
    expect(readdirAt(fd)).toEqual([]);
    expect(fs.readFileSync(path.join(victim, 'survivor.txt')).toString()).toBe('alive');
  });

  it('removes a planted fifo without opening it', () => {
    const ad = newSession();
    const desk = deskPath(ad, 'site', 'd-abcdef');
    fs.mkdirSync(desk, { recursive: true });
    execFileSync('mkfifo', [path.join(desk, 'pipe')]);

    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: false }));
    emptyDesk(fd);
    expect(readdirAt(fd)).toEqual([]);
  });
});

// A behavioural test cannot separate the fd-anchored descent from `fs.rmSync(recursive)`
// on the same procfs path: both unlink a symlink rather than follow it, and the only real
// difference — a component swapped DURING the descent, and `force: true` swallowing a
// removal that failed — is a race no deterministic test can stage. Measured: replacing
// the whole descent with `fs.rmSync` passes every behavioural test above. So this guard
// bans the SHAPE, in the one file whose containment argument depends on its absence.
it('keeps path-based recursive removal out of the desk walker', () => {
  const src = fs.readFileSync(
    new URL('../../../modules/versioned_artifacts/toolbox/desk-io.js', import.meta.url), 'utf8');
  // Comments are stripped first: this file DISCUSSES `fs.rmSync(recursive)` in the
  // rationale for not using it, and a guard that fires on its own prose is a guard that
  // gets weakened until it proves nothing.
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  // Only recursive REMOVAL is banned. `mkdirSync(..., { recursive: true })` on the
  // trusted store side is ordinary, and a guard that also forbade it would be argued
  // down rather than kept.
  expect(code).not.toMatch(/\brmSync\b/);
  expect(code).not.toMatch(/\bfs\.rm\(/);
  expect(code).not.toMatch(/rmdirSync\([^)]*recursive/);
});

describe.skipIf(!linux)('mirrorOut reads agent bytes into the store', () => {
  let tmp;
  const newTmp = () => { tmp = fs.mkdtempSync('/tmp/wt-'); return tmp; };
  const tree = (dir) => {
    const out = [];
    const walk = (d, rel) => {
      for (const e of fs.readdirSync(d, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
        const r = rel ? `${rel}/${e.name}` : e.name;
        if (e.isDirectory()) walk(path.join(d, e.name), r); else out.push(r);
      }
    };
    walk(dir, '');
    return out;
  };
  const desk = (files) => {
    const ad = newSession();
    const d = deskPath(ad, 'site', 'd-abcdef');
    fs.mkdirSync(d, { recursive: true });
    for (const [rel, body] of Object.entries(files)) {
      fs.mkdirSync(path.dirname(path.join(d, rel)), { recursive: true });
      fs.writeFileSync(path.join(d, rel), body);
    }
    return { ad, dir: d, fd: keep(openDesk(ad, 'site', 'd-abcdef', { create: false })) };
  };

  it('copies regular files and nested directories', () => {
    const { fd } = desk({ 'a.txt': 'one', 'sub/b.txt': 'two', 'sub/deep/c.txt': 'three' });
    const dest = newTmp();
    mirrorOut(fd, dest, {});
    expect(tree(dest)).toEqual(['a.txt', 'sub/b.txt', 'sub/deep/c.txt']);
    expect(fs.readFileSync(path.join(dest, 'sub/deep/c.txt')).toString()).toBe('three');
  });

  it('carries a filename containing a tab and a newline', () => {
    const odd = 'we\tird\nname.txt';
    const { fd } = desk({ [odd]: 'kept' });
    const dest = newTmp();
    mirrorOut(fd, dest, {});
    expect(fs.readFileSync(path.join(dest, odd)).toString()).toBe('kept');
  });

  it('refuses a symlink in the desk and copies nothing of it', () => {
    const { dir, fd } = desk({ 'ok.txt': 'x' });
    fs.symlinkSync('/etc/passwd', path.join(dir, 'leak'));
    const dest = newTmp();
    expect(() => mirrorOut(fd, dest, {})).toThrow(new RegExp(ERROR_CODES.SYMLINK_NOT_ALLOWED));
    expect(tree(dest)).not.toContain('leak');
  });

  // The fifo case is deliberately NOT tested in-process. Measured: with O_NONBLOCK
  // removed, this assertion does not fail — the whole vitest run hangs forever in
  // `openSync`, because the blocked open stops the very event loop a timeout would need.
  // A hang reports as a dead CI job with no message. The child-process test at the
  // bottom of this file is the one that can survive the regression it checks for.

  it('never copies a .git out of the desk', () => {
    const { dir, fd } = desk({ 'a.txt': 'x' });
    fs.mkdirSync(path.join(dir, '.git'));
    fs.writeFileSync(path.join(dir, '.git', 'config'), '[core]');
    const dest = newTmp();
    mirrorOut(fd, dest, {});
    expect(tree(dest)).toEqual(['a.txt']);
  });

  it('deletes destination files absent from the desk but keeps .git', () => {
    const { fd } = desk({ 'kept.txt': 'new' });
    const dest = newTmp();
    fs.writeFileSync(path.join(dest, 'stale.txt'), 'old');
    fs.mkdirSync(path.join(dest, 'stale-dir'));
    fs.writeFileSync(path.join(dest, 'stale-dir', 'x'), 'old');
    // The pointer `git worktree add` writes. It is absent from the desk by construction,
    // so a naive mirror destroys the draft.
    fs.writeFileSync(path.join(dest, '.git'), 'gitdir: /srv/x');

    mirrorOut(fd, dest, {});
    expect(tree(dest).sort()).toEqual(['.git', 'kept.txt']);
    expect(fs.readFileSync(path.join(dest, '.git')).toString()).toBe('gitdir: /srv/x');
  });

  it('enforces max_file_size, max_files and max_total_size', () => {
    const big = desk({ 'big.txt': 'x'.repeat(50) });
    expect(() => mirrorOut(big.fd, newTmp(), { max_file_size: 10 }))
      .toThrow(new RegExp(ERROR_CODES.FILE_TOO_LARGE));

    const many = desk({ 'a': '1', 'b': '2', 'c': '3' });
    expect(() => mirrorOut(many.fd, newTmp(), { max_files: 2 }))
      .toThrow(new RegExp(ERROR_CODES.TOO_MANY_FILES));

    const total = desk({ 'a': 'x'.repeat(30), 'b': 'x'.repeat(30) });
    expect(() => mirrorOut(total.fd, newTmp(), { max_total_size: 40 }))
      .toThrow(new RegExp(ERROR_CODES.ARTIFACT_TOO_LARGE));
  });

  it('counts a limit on bytes read, so no file lands when one is refused', () => {
    const { fd } = desk({ 'a.txt': 'x'.repeat(50) });
    const dest = newTmp();
    expect(() => mirrorOut(fd, dest, { max_total_size: 10 }))
      .toThrow(new RegExp(ERROR_CODES.ARTIFACT_TOO_LARGE));
    // The store must not be left holding a partial mirror the commit would then freeze.
    expect(tree(dest)).toEqual([]);
  });
});

describe.skipIf(!linux)('mirrorIn writes store bytes onto the desk', () => {
  it('copies the source tree onto the desk', () => {
    const src = fs.mkdtempSync('/tmp/src-');
    fs.mkdirSync(path.join(src, 'sub'), { recursive: true });
    fs.writeFileSync(path.join(src, 'a.txt'), 'one');
    fs.writeFileSync(path.join(src, 'sub', 'b.txt'), 'two');

    const ad = newSession();
    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: true }));
    mirrorIn(src, fd);

    const desk = deskPath(ad, 'site', 'd-abcdef');
    expect(fs.readFileSync(path.join(desk, 'a.txt')).toString()).toBe('one');
    expect(fs.readFileSync(path.join(desk, 'sub', 'b.txt')).toString()).toBe('two');
  });

  it('filters the top-level .git out of the source', () => {
    const src = fs.mkdtempSync('/tmp/src-');
    fs.mkdirSync(path.join(src, '.git'), { recursive: true });
    fs.writeFileSync(path.join(src, '.git', 'HEAD'), 'ref: x');
    fs.writeFileSync(path.join(src, 'a.txt'), 'one');

    const ad = newSession();
    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: true }));
    mirrorIn(src, fd);
    expect(readdirAt(fd).map((e) => e.name)).toEqual(['a.txt']);
  });

  // O_NOFOLLOW covers the symlink case below; THIS is O_EXCL's own property. Without a
  // test of its own, removing O_EXCL passed the whole suite.
  it('refuses to overwrite an entry that appeared on the desk after it was cleared', () => {
    const src = fs.mkdtempSync('/tmp/src-');
    fs.writeFileSync(path.join(src, 'a.txt'), 'store bytes');

    const ad = newSession();
    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: true }));
    fs.writeFileSync(path.join(deskPath(ad, 'site', 'd-abcdef'), 'a.txt'), 'agent bytes');

    expect(() => mirrorIn(src, fd)).toThrow(new RegExp(ERROR_CODES.STORAGE_OPERATION_FAILED));
    expect(fs.readFileSync(path.join(deskPath(ad, 'site', 'd-abcdef'), 'a.txt')).toString())
      .toBe('agent bytes');
  });

  it('refuses to write through a symlink planted on the desk', () => {
    const src = fs.mkdtempSync('/tmp/src-');
    fs.writeFileSync(path.join(src, 'a.txt'), 'store bytes');

    const ad = newSession();
    const fd = keep(openDesk(ad, 'site', 'd-abcdef', { create: true }));
    const victim = path.join(ad, 'victim.txt');
    fs.writeFileSync(victim, 'untouched');
    fs.symlinkSync(victim, path.join(deskPath(ad, 'site', 'd-abcdef'), 'a.txt'));

    expect(() => mirrorIn(src, fd))
      .toThrow(new RegExp(`${ERROR_CODES.SYMLINK_NOT_ALLOWED}|${ERROR_CODES.STORAGE_OPERATION_FAILED}`));
    expect(fs.readFileSync(victim).toString()).toBe('untouched');
  });
});

// A vitest `{ timeout }` cannot bound this: the timeout is a timer on the very event loop
// a blocking `openSync` stops. Only a child process whose deadline the PARENT enforces can
// prove the refusal happens at all. A regression appears as signal SIGTERM / status null.
describe.skipIf(!linux)('a planted fifo cannot hang the toolbox', () => {
  it('refuses the fifo and exits normally, instead of blocking in open', () => {
    const ad = newSession();
    const desk = deskPath(ad, 'site', 'd-abcdef');
    fs.mkdirSync(desk, { recursive: true });
    execFileSync('mkfifo', [path.join(desk, 'pipe')]);
    const dest = fs.mkdtempSync('/tmp/wt-');
    const mod = new URL('../../../modules/versioned_artifacts/toolbox/desk-io.js', import.meta.url).pathname;

    const child = spawnSync(process.execPath, ['-e', `
      import('${mod}').then(({ openDesk, mirrorOut }) => {
        const fd = openDesk('${ad}', 'site', 'd-abcdef', { create: false });
        try { mirrorOut(fd, '${dest}', {}); process.exit(1); }
        // Exit 3 ONLY for the refusal itself. A bare catch here exits 3 on any error at
        // all — including mirrorOut being undefined — which passed this test while the
        // function did not yet exist.
        catch (e) { process.exit(e && e.code === '${ERROR_CODES.INVALID_PATH}' ? 3 : 4); }
      }).catch(() => process.exit(2));
    `], { timeout: 5000 });

    expect({ status: child.status, signal: child.signal }).toEqual({ status: 3, signal: null });
  });
});
