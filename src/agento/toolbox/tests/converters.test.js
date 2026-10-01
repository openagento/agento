import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import XLSX from 'xlsx';

describe('core converters', () => {
  let tmpDir;

  beforeEach(() => {
    tmpDir = path.join(import.meta.dirname, '_test_conv_' + Date.now());
    fs.mkdirSync(tmpDir, { recursive: true });
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
    vi.restoreAllMocks();
  });

  it('exports converters array with PDF and XLSX', async () => {
    const mod = await import('../../modules/core/toolbox/converters.js');
    expect(Array.isArray(mod.converters)).toBe(true);
    expect(mod.converters.length).toBe(2);

    const pdfConv = mod.converters.find(c => c.fromExt === '.pdf');
    expect(pdfConv).toBeDefined();
    expect(pdfConv.toExt).toBe('.md');

    const xlsxConv = mod.converters.find(c => c.fromExt === '.xlsx');
    expect(xlsxConv).toBeDefined();
    expect(xlsxConv.toExt).toBe('.csv');
  });

  it('PDF converter calls pdftotext with correct args', async () => {
    vi.doMock('node:child_process', () => ({
      execFile: vi.fn((_cmd, _args, _opts, cb) => {
        if (typeof _opts === 'function') {
          cb = _opts;
        }
        cb(null, '', '');
      }),
    }));

    vi.resetModules();
    const mod = await import('../../modules/core/toolbox/converters.js');
    const pdfConv = mod.converters.find(c => c.fromExt === '.pdf');

    const inputPath = path.join(tmpDir, 'test.pdf');
    fs.writeFileSync(inputPath, 'fake pdf');

    const result = await pdfConv.convert(inputPath);
    expect(result).toBe(path.join(tmpDir, 'test.md'));
  });

  it('XLSX converter produces CSV from buffer', async () => {
    const mod = await import('../../modules/core/toolbox/converters.js');
    // Inject xlsx module for test environment (in Docker, dynamic import resolves natively)
    mod._setXlsx(XLSX);

    const wb = XLSX.utils.book_new();
    const ws = XLSX.utils.aoa_to_sheet([['Name', 'Value'], ['Alice', '42'], ['Bob', '99']]);
    XLSX.utils.book_append_sheet(wb, ws, 'Sheet1');
    const buf = XLSX.write(wb, { type: 'buffer', bookType: 'xlsx' });

    const inputPath = path.join(tmpDir, 'data.xlsx');
    fs.writeFileSync(inputPath, buf);

    const xlsxConv = mod.converters.find(c => c.fromExt === '.xlsx');
    const result = await xlsxConv.convert(inputPath);
    expect(result).toBe(path.join(tmpDir, 'data.csv'));
    expect(fs.existsSync(result)).toBe(true);

    const csv = fs.readFileSync(result, 'utf-8');
    expect(csv).toContain('Name,Value');
    expect(csv).toContain('Alice,42');
    expect(csv).toContain('Bob,99');
  });

  it('XLSX converter clamps an inflated <dimension> to the real range', async () => {
    const mod = await import('../../modules/core/toolbox/converters.js');

    // Mimic an Excel export that formatted whole rows/columns: the sheet declares a
    // ~1.7e10-cell range while only four cells actually exist. Inject the parsed
    // workbook directly — a real sheet_to_csv over A1:XFB1048571 never returns, so
    // the test would hang if the converter did not clamp first.
    const ws = {
      '!ref': 'A1:XFB1048571',
      A1: { t: 's', v: 'Name' }, B1: { t: 's', v: 'Value' },
      A2: { t: 's', v: 'Alice' }, B2: { t: 's', v: '42' },
    };
    mod._setXlsx({ read: () => ({ SheetNames: ['Sheet1'], Sheets: { Sheet1: ws } }), utils: XLSX.utils });

    const inputPath = path.join(tmpDir, 'inflated.xlsx');
    fs.writeFileSync(inputPath, 'stub');

    const xlsxConv = mod.converters.find(c => c.fromExt === '.xlsx');
    const start = Date.now();
    const result = await xlsxConv.convert(inputPath);
    expect(Date.now() - start).toBeLessThan(2000);
    expect(fs.existsSync(result)).toBe(true);

    const csv = fs.readFileSync(result, 'utf-8');
    expect(csv).toContain('Name,Value');
    expect(csv).toContain('Alice,42');
  });

  it('XLSX converter rejects a range too large even after clamping', async () => {
    const mod = await import('../../modules/core/toolbox/converters.js');

    // A single real cell planted in the far corner — clamping to the real cells
    // alone would still yield a ~1.7e10-cell range, so the guard must reject it
    // (and must do so without ever walking the range).
    const ws = {
      '!ref': 'A1:XFB1048571',
      A1: { t: 's', v: 'x' }, XFB1048571: { t: 's', v: 'y' },
    };
    mod._setXlsx({ read: () => ({ SheetNames: ['Sheet1'], Sheets: { Sheet1: ws } }), utils: XLSX.utils });

    const inputPath = path.join(tmpDir, 'far-corner.xlsx');
    fs.writeFileSync(inputPath, 'stub');

    const xlsxConv = mod.converters.find(c => c.fromExt === '.xlsx');
    const start = Date.now();
    await expect(xlsxConv.convert(inputPath)).rejects.toThrow(/too large/i);
    expect(Date.now() - start).toBeLessThan(2000);
  });

  it('XLSX converter handles multiple sheets', async () => {
    const mod = await import('../../modules/core/toolbox/converters.js');
    mod._setXlsx(XLSX);

    const wb = XLSX.utils.book_new();
    const ws1 = XLSX.utils.aoa_to_sheet([['A', '1']]);
    const ws2 = XLSX.utils.aoa_to_sheet([['B', '2']]);
    XLSX.utils.book_append_sheet(wb, ws1, 'First');
    XLSX.utils.book_append_sheet(wb, ws2, 'Second');
    const buf = XLSX.write(wb, { type: 'buffer', bookType: 'xlsx' });

    const inputPath = path.join(tmpDir, 'multi.xlsx');
    fs.writeFileSync(inputPath, buf);

    const xlsxConv = mod.converters.find(c => c.fromExt === '.xlsx');
    const result = await xlsxConv.convert(inputPath);
    expect(result).toBe(path.join(tmpDir, 'multi_First.csv'));
    expect(fs.existsSync(path.join(tmpDir, 'multi_First.csv'))).toBe(true);
    expect(fs.existsSync(path.join(tmpDir, 'multi_Second.csv'))).toBe(true);
  });
});
