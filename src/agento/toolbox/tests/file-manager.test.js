import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ConverterRegistry, FileManager } from '../file-manager.js';
import fs from 'node:fs';
import path from 'node:path';

describe('ConverterRegistry', () => {
  let registry;

  beforeEach(() => {
    registry = new ConverterRegistry();
  });

  it('registers and retrieves a converter', () => {
    const converter = { fromExt: '.pdf', toExt: '.md', convert: vi.fn() };
    registry.register(converter);
    const result = registry.get('.pdf');
    expect(result).not.toBeNull();
    expect(result.fromExt).toBe('.pdf');
    expect(result.toExt).toBe('.md');
  });

  it('returns null for unregistered extension', () => {
    expect(registry.get('.xyz')).toBeNull();
  });

  it('has() returns true for registered and false for unregistered', () => {
    registry.register({ fromExt: '.pdf', toExt: '.md', convert: vi.fn() });
    expect(registry.has('.pdf')).toBe(true);
    expect(registry.has('.xyz')).toBe(false);
  });

  it('normalizes extensions to lowercase', () => {
    registry.register({ fromExt: '.PDF', toExt: '.md', convert: vi.fn() });
    expect(registry.has('.pdf')).toBe(true);
    expect(registry.get('.PDF')).not.toBeNull();
  });

  it('all() returns a snapshot of registered converters', () => {
    registry.register({ fromExt: '.pdf', toExt: '.md', convert: vi.fn() });
    registry.register({ fromExt: '.xlsx', toExt: '.csv', convert: vi.fn() });
    const all = registry.all();
    expect(all.size).toBe(2);
    expect(all.has('.pdf')).toBe(true);
    expect(all.has('.xlsx')).toBe(true);
  });

  it('clear() removes all converters', () => {
    registry.register({ fromExt: '.pdf', toExt: '.md', convert: vi.fn() });
    registry.clear();
    expect(registry.has('.pdf')).toBe(false);
    expect(registry.all().size).toBe(0);
  });
});

describe('FileManager', () => {
  let tmpDir;
  let registry;

  beforeEach(() => {
    tmpDir = path.join(import.meta.dirname, '_test_fm_' + Date.now());
    fs.mkdirSync(tmpDir, { recursive: true });
    registry = new ConverterRegistry();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
    vi.restoreAllMocks();
  });

  function createFileManager(opts = {}) {
    return new FileManager({
      converterRegistry: registry,
      allowedExtensions: opts.allowedExtensions || new Set(['.pdf', '.png', '.jpg', '.xlsx', '.csv', '.txt']),
      maxFileSize: opts.maxFileSize || 10_000_000,
      log: opts.log,
    });
  }

  it('skips disallowed extensions', async () => {
    const fm = createFileManager();
    const result = await fm.download('http://example.com/file.exe', 'file.exe', { dir: tmpDir });
    expect(result.skipped).toBe(true);
    expect(result.skipReason).toContain('.exe');
    expect(result.skipReason).toContain('not allowed');
  });

  it('skips oversized files', async () => {
    const fm = createFileManager({ maxFileSize: 1000 });
    const result = await fm.download('http://example.com/file.pdf', 'file.pdf', {
      dir: tmpDir,
      maxSize: 5000,
    });
    expect(result.skipped).toBe(true);
    expect(result.skipReason).toContain('too large');
  });

  it('downloads and returns localPath', async () => {
    const fm = createFileManager();
    const fakeContent = new globalThis.TextEncoder().encode('hello world');

    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(fakeContent.buffer.slice(fakeContent.byteOffset, fakeContent.byteOffset + fakeContent.byteLength)),
    });

    const result = await fm.download('http://example.com/file.txt', 'file.txt', { dir: tmpDir });
    expect(result.skipped).toBe(false);
    expect(result.localPath).toContain('file_');
    expect(result.localPath.endsWith('.txt')).toBe(true);
    expect(result.convertedPath).toBeNull();

    const content = fs.readFileSync(result.localPath);
    expect(content.toString()).toBe('hello world');
  });

  it('converts when converter is registered', async () => {
    const convertFn = vi.fn().mockResolvedValue('/converted/file.md');
    registry.register({ fromExt: '.pdf', toExt: '.md', convert: convertFn });
    const fm = createFileManager();

    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(Buffer.from('pdf content').buffer),
    });

    const result = await fm.download('http://example.com/doc.pdf', 'doc.pdf', { dir: tmpDir });
    expect(result.skipped).toBe(false);
    expect(result.convertedPath).toBe('/converted/file.md');
    expect(convertFn).toHaveBeenCalledOnce();
    expect(convertFn.mock.calls[0][0]).toContain('doc_');
  });

  it('returns convertedPath=null when no converter registered', async () => {
    const fm = createFileManager();

    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(Buffer.from('png data').buffer),
    });

    const result = await fm.download('http://example.com/image.png', 'image.png', { dir: tmpDir });
    expect(result.skipped).toBe(false);
    expect(result.localPath).toContain('.png');
    expect(result.convertedPath).toBeNull();
  });

  it('handles conversion failure gracefully', async () => {
    registry.register({
      fromExt: '.pdf',
      toExt: '.md',
      convert: vi.fn().mockRejectedValue(new Error('pdftotext not found')),
    });
    const fm = createFileManager();

    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(Buffer.from('pdf').buffer),
    });

    const result = await fm.download('http://example.com/doc.pdf', 'doc.pdf', { dir: tmpDir });
    expect(result.skipped).toBe(false);
    expect(result.localPath).toContain('.pdf');
    expect(result.convertedPath).toBeNull();
    expect(result.conversionError).toBe('pdftotext not found');
  });

  it('handles download failure', async () => {
    const fm = createFileManager();

    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 404,
    });

    const result = await fm.download('http://example.com/file.txt', 'file.txt', { dir: tmpDir });
    expect(result.skipped).toBe(true);
    expect(result.skipReason).toContain('HTTP 404');
  });

  it('passes headers to fetch', async () => {
    const fm = createFileManager();
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(Buffer.from('data').buffer),
    });

    const headers = { Authorization: 'Basic abc123' };
    await fm.download('http://example.com/file.txt', 'file.txt', { dir: tmpDir, headers });
    expect(fetchSpy).toHaveBeenCalledWith('http://example.com/file.txt', { headers });
  });

  it('exposes converterRegistry via getter', () => {
    const fm = createFileManager();
    expect(fm.converterRegistry).toBe(registry);
  });

  it('logs and returns skipReason on fetch error', async () => {
    const logSpy = vi.fn();
    const fm = createFileManager({ log: logSpy });

    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('ECONNREFUSED'));

    const result = await fm.download('http://example.com/file.txt', 'file.txt', { dir: tmpDir });
    expect(result.skipped).toBe(true);
    expect(result.skipReason).toMatch(/Write failed/);
    expect(logSpy).toHaveBeenCalledWith('file_manager', 'ERROR', expect.stringContaining('Write failed'));
    expect(logSpy).toHaveBeenCalledWith('file_manager', 'ERROR', expect.stringContaining('ECONNREFUSED'));
  });

  it('returns conversionError when converter throws', async () => {
    registry.register({
      fromExt: '.xlsx',
      toExt: '.csv',
      convert: vi.fn().mockRejectedValue(new Error('Bad archive')),
    });
    const fm = createFileManager();

    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(Buffer.from('xlsx data').buffer),
    });

    const result = await fm.download('http://example.com/data.xlsx', 'data.xlsx', { dir: tmpDir });
    expect(result.skipped).toBe(false);
    expect(result.convertedPath).toBeNull();
    expect(result.conversionError).toBe('Bad archive');
  });

  function okFetch(text = 'body') {
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async () => ({
      ok: true,
      arrayBuffer: async () => new globalThis.TextEncoder().encode(text).buffer,
    }));
  }

  it('name sets the exact file name and leaves no .part entry behind', async () => {
    okFetch('hello');
    const fm = createFileManager();
    const result = await fm.download('http://x/a.txt', 'a.txt', { dir: tmpDir, root: tmpDir, name: '10_a.txt' });
    expect(result.localPath).toBe(path.join(tmpDir, '10_a.txt'));
    expect(fs.readFileSync(result.localPath, 'utf8')).toBe('hello');
    expect(fs.readdirSync(tmpDir)).toEqual(['10_a.txt']);
  });

  it.each(['../x.txt', 'a/b.txt', '..', ''])('refuses the name %j', async (name) => {
    const fetchSpy = okFetch();
    const result = await createFileManager().download('http://x/a.txt', 'a.txt', { dir: tmpDir, root: tmpDir, name });
    expect(result.skipped).toBe(true);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each([{ name: '1_a.txt' }, { skipExisting: true }])('in a run dir (no root) refuses %j', async (opts) => {
    const fetchSpy = okFetch();
    const result = await createFileManager().download('http://x/a.txt', 'a.txt', { dir: tmpDir, ...opts });
    expect(result).toEqual({ skipped: true, skipReason: 'name and skipExisting need root' });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('in a run dir (no root) two files with one name in one millisecond both land', async () => {
    okFetch('x');
    vi.spyOn(Date, 'now').mockReturnValue(5);
    const fm = createFileManager();
    const [a, b] = await Promise.all([
      fm.download('http://x/1', 'image.png', { dir: tmpDir }), fm.download('http://x/2', 'image.png', { dir: tmpDir })]);
    expect(a.skipped).toBe(false);
    expect(b.skipped).toBe(false);
    expect(a.localPath).not.toBe(b.localPath);
  });

  it('in a run dir (no root) a symlink planted at the file name is not followed', async () => {
    const victim = path.join(tmpDir, 'victim.txt');
    fs.writeFileSync(victim, 'keep');
    const dir = path.join(tmpDir, 'run');
    fs.mkdirSync(dir);
    vi.spyOn(Date, 'now').mockReturnValue(7);
    vi.spyOn(globalThis.crypto, 'getRandomValues').mockImplementation((a) => a.fill(0));
    fs.symlinkSync(victim, path.join(dir, 'a_7_00000000.txt'));
    okFetch('ATTACKER');
    const result = await createFileManager().download('http://x/a.txt', 'a.txt', { dir });
    expect(result.skipped).toBe(true);
    expect(fs.readFileSync(victim, 'utf8')).toBe('keep');
  });

  it('skipExisting returns the file that is there with no fetch', async () => {
    const fetchSpy = okFetch('first');
    const fm = createFileManager();
    const opts = { dir: tmpDir, root: tmpDir, name: '10_a.txt', skipExisting: true };
    const first = await fm.download('http://x/a.txt', 'a.txt', opts);
    const second = await fm.download('http://x/a.txt', 'a.txt', opts);
    expect(second).toEqual(first);
    expect(fetchSpy).toHaveBeenCalledOnce();
  });

  it('skipExisting fetches again while the conversion is missing', async () => {
    const convert = vi.fn(async (p) => { fs.writeFileSync(p.replace(/\.pdf$/, '.md'), 'md'); return p.replace(/\.pdf$/, '.md'); });
    registry.register({ fromExt: '.pdf', toExt: '.md', convert });
    const fetchSpy = okFetch();
    const fm = createFileManager();
    const opts = { dir: tmpDir, root: tmpDir, name: '10_d.pdf', skipExisting: true };
    const first = await fm.download('http://x/d.pdf', 'd.pdf', opts);
    expect(first.convertedPath).toBe(path.join(tmpDir, '10_d.md'));
    expect(fs.readFileSync(first.convertedPath, 'utf8')).toBe('md');
    expect(await fm.download('http://x/d.pdf', 'd.pdf', opts)).toEqual(first);
    expect(fetchSpy).toHaveBeenCalledOnce();
    fs.rmSync(first.convertedPath);
    await fm.download('http://x/d.pdf', 'd.pdf', opts);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('two downloads of one file at once both end with the whole file', async () => {
    okFetch('same bytes');
    const fm = createFileManager();
    const opts = { dir: tmpDir, root: tmpDir, name: '10_a.txt', skipExisting: true };
    const [a, b] = await Promise.all([
      fm.download('http://x/a.txt', 'a.txt', opts), fm.download('http://x/a.txt', 'a.txt', opts)]);
    expect(a.localPath).toBe(b.localPath);
    expect(fs.readFileSync(a.localPath, 'utf8')).toBe('same bytes');
    expect(fs.readdirSync(tmpDir)).toEqual(['10_a.txt']);
  });

  it('skipExisting reuses a conversion with its own name (one CSV per XLSX sheet)', async () => {
    const convert = vi.fn(async (p) => {
      const base = p.replace(/\.xlsx$/, '');
      fs.writeFileSync(`${base}_Sheet2.csv`, 'b');
      fs.writeFileSync(`${base}_Sheet1.csv`, 'a');
      return `${base}_Sheet1.csv`;
    });
    registry.register({ fromExt: '.xlsx', toExt: '.csv', convert });
    const fetchSpy = okFetch();
    const fm = createFileManager();
    const opts = { dir: tmpDir, root: tmpDir, name: '10_d.xlsx', skipExisting: true };
    const first = await fm.download('http://x/d.xlsx', 'd.xlsx', opts);
    expect(first.convertedPath).toBe(path.join(tmpDir, '10_d_Sheet1.csv'));
    expect(fs.existsSync(path.join(tmpDir, '10_d_Sheet2.csv'))).toBe(true);
    expect(await fm.download('http://x/d.xlsx', 'd.xlsx', opts)).toEqual(first);
    expect(fetchSpy).toHaveBeenCalledOnce();
  });

  it('in a run dir (no root) nothing there is listed or moved: a swapped dir is never emptied', async () => {
    // A run owns its run dir and could swap a dir the toolbox lists for a link to another
    // run's dir; without root the toolbox writes the file in place and moves nothing.
    const other = path.join(tmpDir, 'other-run');
    fs.mkdirSync(other);
    fs.writeFileSync(path.join(other, 'secret.txt'), 's');
    const dir = path.join(tmpDir, 'run', 'jira', 'AI-1');
    fs.mkdirSync(dir, { recursive: true });
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      fs.symlinkSync(other, path.join(dir, '.part-planted'));
      return { ok: true, arrayBuffer: async () => new globalThis.TextEncoder().encode('x').buffer };
    });
    vi.spyOn(Date, 'now').mockReturnValue(1);
    vi.spyOn(globalThis.crypto, 'getRandomValues').mockImplementation((a) => a.fill(0));
    const result = await createFileManager().download('http://x/a.txt', 'a.txt', { dir });
    expect(result.skipped).toBe(false);
    expect(fs.readdirSync(other)).toEqual(['secret.txt']);
    expect(fs.readdirSync(dir).sort()).toEqual(['.part-planted', 'a_1_00000000.txt']);
  });

  it('under root: makes the missing dirs 0750 and the file 0640', async () => {
    okFetch();
    const root = path.join(tmpDir, 'downloads');
    fs.mkdirSync(root);
    const dir = path.join(root, 'jira', 'AI-1');
    const result = await createFileManager().download('http://x/a.txt', 'a.txt', { dir, root, name: '1_a.txt' });
    expect(result.skipped).toBe(false);
    expect(fs.statSync(path.join(root, 'jira')).mode & 0o777).toBe(0o750);
    expect(fs.statSync(dir).mode & 0o777).toBe(0o750);
    expect(fs.statSync(result.localPath).mode & 0o777).toBe(0o640);
  });

  it.each(['root', 'jira', 'AI-1'])('under root: refuses a planted symlink at %s', async (at) => {
    const fetchSpy = okFetch();
    const elsewhere = path.join(tmpDir, 'elsewhere');
    fs.mkdirSync(elsewhere);
    const real = path.join(tmpDir, 'downloads');
    let root = real;
    if (at === 'root') {
      root = path.join(tmpDir, 'link');
      fs.symlinkSync(elsewhere, root);
    } else {
      fs.mkdirSync(real);
      if (at === 'jira') fs.symlinkSync(elsewhere, path.join(real, 'jira'));
      else { fs.mkdirSync(path.join(real, 'jira')); fs.symlinkSync(elsewhere, path.join(real, 'jira', 'AI-1')); }
    }
    const dir = path.join(root, 'jira', 'AI-1');
    const result = await createFileManager().download('http://x/a.txt', 'a.txt', { dir, root, name: '1_a.txt' });
    expect(result.skipped).toBe(true);
    expect(result.skipReason).toMatch(/symlink/);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(fs.readdirSync(elsewhere)).toEqual([]);
  });

  it('under root: refuses a dir outside it', async () => {
    okFetch();
    const root = path.join(tmpDir, 'downloads');
    fs.mkdirSync(root);
    const result = await createFileManager().download('http://x/a.txt', 'a.txt',
      { dir: path.join(root, '..', 'other'), root, name: '1_a.txt' });
    expect(result.skipped).toBe(true);
  });

  it('logs converter errors via injected log function', async () => {
    const logSpy = vi.fn();
    registry.register({
      fromExt: '.xlsx',
      toExt: '.csv',
      convert: vi.fn().mockRejectedValue(new Error('Parse error')),
    });
    const fm = createFileManager({ log: logSpy });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(Buffer.from('xlsx').buffer),
    });

    await fm.download('http://example.com/sheet.xlsx', 'sheet.xlsx', { dir: tmpDir });
    expect(logSpy).toHaveBeenCalledWith('file_manager', 'ERROR', expect.stringContaining('Parse error'));
  });
});
