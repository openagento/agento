import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, writeFile } from 'node:fs/promises';
import { basename, dirname, join } from 'node:path';

const execFileAsync = promisify(execFile);

async function convertPdf(inputPath) {
  const outputPath = inputPath.replace(/\.pdf$/i, '.md');
  await execFileAsync('pdftotext', ['-layout', inputPath, outputPath], { timeout: 30_000 });
  return outputPath;
}

let _xlsxModule = null;

export function _setXlsx(mod) {
  _xlsxModule = mod;
}

async function loadXlsx() {
  if (_xlsxModule) return _xlsxModule;
  const m = await import('xlsx');
  return m.default || m;
}

// sheet_to_csv walks the sheet's declared range (ws['!ref']), not just the cells
// that exist. An XLSX exported after whole rows/columns were formatted declares a
// dimension like A1:XFB1048571 (~1.7e10 cells) for a few hundred real cells, so the
// conversion runs effectively forever and blocks the single toolbox event loop.
// Clamp the range to the cells that actually exist, then refuse a range that is
// still pathologically large (e.g. one real cell planted in the far corner, which
// clamping alone would not catch) so the work stays bounded.
const MAX_CELLS = 5_000_000;

function clampSheetRange(XLSX, ws) {
  let maxRow = 0;
  let maxCol = 0;
  for (const key of Object.keys(ws)) {
    if (key[0] === '!') continue;
    const cell = XLSX.utils.decode_cell(key);
    if (cell.r > maxRow) maxRow = cell.r;
    if (cell.c > maxCol) maxCol = cell.c;
  }
  const cellCount = (maxRow + 1) * (maxCol + 1);
  if (cellCount > MAX_CELLS) {
    throw new Error(`XLSX sheet range too large to convert (${cellCount} cells)`);
  }
  ws['!ref'] = XLSX.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: maxRow, c: maxCol } });
}

async function convertXlsx(inputPath) {
  const XLSX = await loadXlsx();
  const buf = await readFile(inputPath);
  const wb = XLSX.read(buf, { type: 'buffer' });
  const dir = dirname(inputPath);
  const base = basename(inputPath).replace(/\.xlsx$/i, '');
  const results = [];

  for (const name of wb.SheetNames) {
    clampSheetRange(XLSX, wb.Sheets[name]);
    const csv = XLSX.utils.sheet_to_csv(wb.Sheets[name]);
    const dest = wb.SheetNames.length === 1
      ? join(dir, `${base}.csv`)
      : join(dir, `${base}_${name}.csv`);
    await writeFile(dest, csv, 'utf-8');
    results.push(dest);
  }
  return results[0];
}

export const converters = [
  { fromExt: '.pdf', toExt: '.md', convert: convertPdf },
  { fromExt: '.xlsx', toExt: '.csv', convert: convertXlsx },
];
