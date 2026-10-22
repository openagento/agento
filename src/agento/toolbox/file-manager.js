import {
  chmod, lstat, mkdir, mkdtemp, readdir, readFile, rename, rm, writeFile,
} from 'node:fs/promises';
import {
  basename, dirname, extname, isAbsolute, join, relative, sep,
} from 'node:path';

export class ConverterRegistry {
  #converters = new Map();

  register({ fromExt, toExt, convert }) {
    const ext = fromExt.toLowerCase();
    this.#converters.set(ext, { fromExt: ext, toExt, convert });
  }

  get(ext) {
    return this.#converters.get(ext.toLowerCase()) || null;
  }

  has(ext) {
    return this.#converters.has(ext.toLowerCase());
  }

  all() {
    return new Map(this.#converters);
  }

  clear() {
    this.#converters.clear();
  }
}

export class FileManager {
  #converterRegistry;
  #allowedExtensions;
  #maxFileSize;
  #log;

  constructor({ converterRegistry, allowedExtensions, maxFileSize, log }) {
    this.#converterRegistry = converterRegistry;
    this.#allowedExtensions = allowedExtensions;
    this.#maxFileSize = maxFileSize;
    this.#log = log || (() => {});
  }

  get converterRegistry() {
    return this.#converterRegistry;
  }

  /**
   * Download `url` into `dir` and convert it when a converter knows its extension.
   *
   * - `name`: the exact file name (one path segment). Default: `<base>_<Date.now()>_<8 random hex><ext>`.
   * - `skipExisting`: under `root`, a file `name` that is already there (and its
   *   conversion, when a converter applies) is returned with no fetch.
   * - `root`: a tool-owned tree `dir` lies in (a thread's `downloads/`, 2750, group
   *   THREAD_GID). Each component below it is checked with `lstat` and a symlink is
   *   refused; a missing one is made with mode 0750 and takes the group and the setgid
   *   bit of its parent. The files get 0640: a run of the thread reads them and changes
   *   nothing (SEC-5).
   *
   * Under `root` the body and its conversion are written in a fresh `.part-*` dir in `dir`
   * and renamed into place, the file last, with a `.<name>.converted` note naming the
   * conversion: two sessions that fetch one file get the same result, and a file that is
   * there is complete. Without `root` (`dir` is in a run dir, which the run can change)
   * the toolbox lists and moves nothing there: it writes a new file with a generated name
   * (`wx`, never through a planted entry) and converts it in place; `name` and
   * `skipExisting` are refused there.
   */
  async download(url, filename, { headers, dir, maxSize, name, skipExisting = false, root } = {}) {
    const ext = extname(filename).toLowerCase();
    if (!this.#allowedExtensions.has(ext)) {
      return { skipped: true, skipReason: `Extension ${ext} not allowed` };
    }
    if (maxSize && maxSize > this.#maxFileSize) {
      return { skipped: true, skipReason: `File too large (${maxSize} bytes)` };
    }
    // A chosen name in a dir the run can change is a target the run can plant a symlink
    // at; only a tool-owned `root` tree may take one (SEC-5).
    if (!root && (name !== undefined || skipExisting)) {
      return { skipped: true, skipReason: 'name and skipExisting need root' };
    }
    const fileName = name ?? `${basename(filename, ext)}_${Date.now()}_${randomHex()}${ext}`;
    if (!isOneSegment(fileName)) {
      return { skipped: true, skipReason: `Invalid file name ${JSON.stringify(fileName)}` };
    }
    const converter = this.#converterRegistry.get(ext);
    const localPath = join(dir, fileName);
    const notePath = join(dir, `.${fileName}.converted`);

    let partDir = null;
    let convertedPath = null;
    let conversionError = null;
    const convert = async (input) => {
      if (!converter) return null;
      try {
        const out = await converter.convert(input);
        this.#log('file_manager', 'OK', `Converted ${filename} -> ${out}`);
        return out;
      } catch (err) {
        conversionError = err.message;
        this.#log('file_manager', 'ERROR', `Conversion failed: ${filename}: ${err.message}`);
        return null;
      }
    };
    try {
      if (root) await ensureTree(root, dir, 0o750);
      else await mkdir(dir, { recursive: true });
      if (skipExisting && root) {
        const existing = await existingResult(dir, localPath, notePath, converter);
        if (existing) return existing;
      }
      const res = await fetch(url, { headers });
      if (!res.ok) {
        this.#log('file_manager', 'ERROR', `Download failed: ${filename} HTTP ${res.status}`);
        return { skipped: true, skipReason: `Download failed: HTTP ${res.status}` };
      }
      const body = Buffer.from(await res.arrayBuffer());
      if (!root) {
        // `wx`: an entry the run planted at the name fails the write, it is not followed.
        await writeFile(localPath, body, { flag: 'wx' });
        convertedPath = await convert(localPath);
      } else {
        // The `.part-*` dir is the toolbox's own, in a tree no run can write: listing it
        // and moving what is in it reaches nothing else (SEC-5).
        partDir = await mkdtemp(join(dir, '.part-'));
        const partPath = join(partDir, fileName);
        await writeFile(partPath, body, { flag: 'wx', mode: 0o640 });
        const out = await convert(partPath);
        convertedPath = out && dirname(out) === partDir ? join(dir, basename(out)) : out;
        for (const entry of (await readdir(partDir)).filter((e) => e !== fileName)) {
          await chmod(join(partDir, entry), 0o640);
          await rename(join(partDir, entry), join(dir, entry));
        }
        if (convertedPath && dirname(convertedPath) === dir) {
          const note = join(partDir, 'note');
          await writeFile(note, basename(convertedPath), { flag: 'wx', mode: 0o640 });
          await rename(note, notePath);
        }
        await rename(partPath, localPath);
      }
    } catch (err) {
      this.#log('file_manager', 'ERROR', `Write failed: ${filename} -> ${localPath}: ${err.message}`);
      return { skipped: true, skipReason: `Write failed: ${err.message}` };
    } finally {
      if (partDir) await rm(partDir, { recursive: true, force: true });
    }

    return { localPath, convertedPath, conversionError, skipped: false };
  }
}

// 8 hex chars for a generated file name. Through `globalThis.crypto` so a test can fix it.
function randomHex() {
  return Buffer.from(globalThis.crypto.getRandomValues(new Uint8Array(4))).toString('hex');
}

/** A file name with no separator, no NUL, and not `.`/`..`. */
function isOneSegment(fileName) {
  return typeof fileName === 'string' && fileName !== '' && fileName !== '.' && fileName !== '..'
    && !/[/\\\0]/.test(fileName);
}

/** `localPath` as a finished download, or null when it is not there, or when a converter
 * applies and the note naming its output (or that output) is not there: the next download
 * then makes both. The note holds the output's name, so a converter may name it freely (one
 * CSV per XLSX sheet, the first returned). */
async function existingResult(dir, localPath, notePath, converter) {
  if (!(await isPlainFile(localPath))) return null;
  let convertedPath = null;
  if (converter) {
    if (!(await isPlainFile(notePath))) return null;
    const name = (await readFile(notePath, 'utf8')).trim();
    if (!isOneSegment(name)) return null;
    convertedPath = join(dir, name);
    if (!(await isPlainFile(convertedPath))) return null;
  }
  return { localPath, convertedPath, conversionError: null, skipped: false };
}

async function isPlainFile(path) {
  try {
    return (await lstat(path)).isFile();
  } catch (err) {
    if (err.code === 'ENOENT') return false;
    throw err;
  }
}

/** Make `dir` under `root`, one component at a time. `root` and each component must be a
 * directory, not a symlink; a missing one is made with `mode` (Linux adds the setgid bit
 * and the group of a setgid parent). */
async function ensureTree(root, dir, mode) {
  const rel = relative(root, dir);
  if (rel.startsWith('..') || isAbsolute(rel)) throw new Error(`${dir} is not under ${root}`);
  let current = root;
  for (const segment of ['', ...rel.split(sep).filter(Boolean)]) {
    current = segment ? join(current, segment) : current;
    let st;
    try {
      st = await lstat(current);
    } catch (err) {
      if (err.code !== 'ENOENT' || !segment) throw err;
      try {
        // No chmod: the new dir takes the group and the setgid bit of `root` from its
        // parent, and a chmod by a process outside that group would clear the bit.
        await mkdir(current, { mode });
      } catch (mkErr) {
        if (mkErr.code !== 'EEXIST') throw mkErr;
      }
      st = await lstat(current);
    }
    if (st.isSymbolicLink() || !st.isDirectory()) {
      throw new Error(`${current} is a symlink or not a directory`);
    }
  }
}
