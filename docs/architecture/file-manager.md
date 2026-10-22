# FileManager

Framework-level gateway for all external files entering the Agento system through the Toolbox.

## Security Mandate

All external files MUST go through FileManager. Direct `fetch` + `writeFile` in module code is forbidden for external content. This ensures:

- Extension allow-listing is enforced centrally
- File size limits cannot be bypassed
- Future security checks (malware scanning, content validation) apply to all files automatically

## Pipeline

Every file download follows a three-step pipeline:

1. **Validate** -- check filename extension against the allowed list and file size against the configured maximum. Rejected files return `{ skipped: true, skipReason: "..." }`.
2. **Download** -- fetch the file from the URL and write it to disk.
3. **Convert** -- if a converter is registered for the file extension, run it to produce a text-friendly format (e.g., PDF to Markdown, XLSX to CSV). Conversion failures are non-fatal; the original file remains available.

### Options

`download(url, filename, { headers, dir, maxSize, name, skipExisting, root })`:

- `dir` -- the dir the file goes into. Without `root` it is a dir the run can change (its run
  dir): the file gets a generated name (`<base>_<Date.now()>_<8 random hex><ext>`: two files with one name never collide, and a run cannot guess it), is written with `wx` (an entry
  planted at the name fails the write, it is not followed) and is converted in place. Nothing in
  that dir is listed or moved.
- `root` -- a tool-owned tree that `dir` lies in, such as a thread's `downloads/` (2750, group
  THREAD_GID). Each component of `dir` below `root` is checked with `lstat`; a symlink, a non-dir
  or a dir outside `root` is refused. A missing dir is made 0750 and takes the setgid bit and the
  group of its parent. Files are 0640.
- `name` -- the exact file name (one path segment). Needs `root`.
- `skipExisting` -- return the file that is already there (and its conversion) with no fetch.
  Needs `root`.

Under `root` the body is written in a fresh `.part-*` dir in `dir` and converted there. The
converter's outputs (in the input's dir) are renamed into `dir`, then a `.<name>.converted` note
that names the conversion, then the file last; the `.part-*` dir is removed after. A file that is
there is complete, and `skipExisting` returns it only while the noted conversion is there too.
A converter that writes outside the input's dir is not moved.

## Converter Interface

Every converter must implement:

```javascript
{
  fromExt: '.pdf',           // source extension (lowercase, with dot)
  toExt: '.md',              // target extension
  convert(srcPath) -> string // async, returns path to converted file
}
```

## Converter Registry

The `ConverterRegistry` manages converter registrations:

- `register({ fromExt, toExt, convert })` -- register a converter
- `get(ext)` -- retrieve converter for an extension (or null)
- `has(ext)` -- check if a converter exists
- `all()` -- return a Map snapshot of all registered converters
- `clear()` -- remove all (used in tests)

## Configuration

Two config paths in the `core` module (3-level fallback applies):

| Config Path | Type | Default | Description |
|---|---|---|---|
| `toolbox/file_manager/allowed_extensions` | string | `.pdf,.xlsx,.xls,.csv,.txt,.md,.json,.xml,.html,.png,.jpg,.jpeg,.gif,.svg,.webp` | Comma-separated list of allowed file extensions |
| `toolbox/file_manager/max_file_size` | integer | `524288000` (500 MB) | Maximum file size in bytes |

Override via ENV: `CONFIG__CORE__TOOLBOX/FILE_MANAGER/ALLOWED_EXTENSIONS`
Override via DB: `config:set core/toolbox/file_manager/allowed_extensions ".pdf,.png"`

## Built-in Converters

Shipped in `src/agento/modules/core/toolbox/converters.js`:

| From | To | Tool | Notes |
|---|---|---|---|
| `.pdf` | `.md` | `pdftotext` (poppler-utils) | Layout-preserving text extraction |
| `.xlsx` | `.csv` | `xlsx` npm package | Multi-sheet: produces one CSV per sheet, returns first |

## Adding a Custom Converter

Create a module with a `toolbox/converters.js` file that exports a `converters` array:

```javascript
// modules/my-module/toolbox/converters.js
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

async function convertDocx(inputPath) {
  const outputPath = inputPath.replace(/\.docx$/i, '.txt');
  await execFileAsync('pandoc', [inputPath, '-t', 'plain', '-o', outputPath]);
  return outputPath;
}

export const converters = [
  { fromExt: '.docx', toExt: '.txt', convert: convertDocx },
];
```

The config-loader auto-discovers the `converters` export during module loading and registers them with the FileManager's converter registry. No framework code changes needed.

## Usage in Module Tools

Module tools receive `fileManager` via the context object:

```javascript
export function register(server, { fileManager, runtimeDir }) {
  server.tool('my_tool', '...', {}, async () => {
    const result = await fileManager.download(url, filename, {
      headers: authHeaders,
      dir: `${runtimeDir}/my-module/files`,
      maxSize: attachment.size,
    });

    if (result.skipped) {
      // File was rejected (bad extension, too large, download failed)
      // result.skipReason contains the reason -- surface it to the agent
      return { error: result.skipReason };
    }

    // result.localPath       -- path to downloaded file
    // result.convertedPath   -- path to converted file (or null)
    // result.conversionError -- error message if conversion failed (or null)
    // Always surface conversionError to the agent so it can report it,
    // rather than silently returning a binary the agent cannot read.
    return {
      localPath: result.localPath,
      convertedPath: result.convertedPath,
      error: result.conversionError || null,
    };
  });
}
```
