import { writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { logToolboxMcp as log } from '../log.js';

function escapeCsvField(value) {
  if (value === null || value === undefined) return '';
  const str = String(value);
  if (str.includes(',') || str.includes('"') || str.includes('\n')) {
    return '"' + str.replace(/"/g, '""') + '"';
  }
  return str;
}

export function rowsToCsv(rows) {
  if (rows.length === 0) return '';
  const columns = Object.keys(rows[0]);
  const header = columns.join(',');
  const lines = rows.map(row => columns.map(col => escapeCsvField(row[col])).join(','));
  return header + '\n' + lines.join('\n');
}

function formatDatetime() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

function resultFileName(ext) {
  return `result_${formatDatetime()}_${randomBytes(4).toString('hex')}.${ext}`;
}

export async function maybeOffloadRows(rows, toolName, { artifactsDir, threshold, sampleRows }) {
  if (!artifactsDir) return null;
  const estimatedSize = JSON.stringify(rows).length;
  if (estimatedSize <= threshold) return null;

  const dir = join(artifactsDir, 'mcp-results', toolName);
  const filePath = join(dir, resultFileName('csv'));

  try {
    await mkdir(dir, { recursive: true });

    const csv = rowsToCsv(rows);
    await writeFile(filePath, csv);

    const columns = Object.keys(rows[0]);
    const sampleCsv = rowsToCsv(rows.slice(0, sampleRows));
    const summary = [
      `Query returned ${rows.length} rows (~${Math.round(estimatedSize / 1024)} KB). Full results saved to:`,
      filePath,
      '',
      `Columns: ${columns.join(', ')}`,
      `First ${Math.min(sampleRows, rows.length)} rows:`,
      sampleCsv,
    ].join('\n');

    return { summary, filePath };
  } catch (err) {
    log('large-result', 'WARN', `Failed to offload rows for ${toolName}: ${err.message}`);
    return null;
  }
}

export async function maybeOffloadText(text, toolName, { artifactsDir, threshold, textPreviewChars }) {
  if (!artifactsDir) return null;
  if (text.length <= threshold) return null;

  const dir = join(artifactsDir, 'mcp-results', toolName);
  const filePath = join(dir, resultFileName('txt'));

  try {
    await mkdir(dir, { recursive: true });

    await writeFile(filePath, text);

    const preview = text.slice(0, textPreviewChars);
    const summary = [
      `Result too large (${text.length} chars, ~${Math.round(text.length / 1024)} KB). Full output saved to:`,
      filePath,
      '',
      `First ${textPreviewChars} chars:`,
      preview,
    ].join('\n');

    return { summary, filePath };
  } catch (err) {
    log('large-result', 'WARN', `Failed to offload text for ${toolName}: ${err.message}`);
    return null;
  }
}

export function wrapHandler(handler, toolName, strategy, offloadConfig) {
  if (strategy === false) return handler;

  return async (args) => {
    const result = await handler(args);
    if (result.isError || !offloadConfig.artifactsDir) return result;

    const textItems = result.content?.filter(c => c.type === 'text') || [];
    const textContent = textItems.map(c => c.text).join('\n');
    if (textContent.length <= offloadConfig.threshold) return result;

    let offloaded = null;

    if (strategy === 'rows') {
      for (const item of textItems) {
        try {
          const parsed = JSON.parse(item.text);
          if (Array.isArray(parsed) && parsed.length > 0 && typeof parsed[0] === 'object') {
            if (JSON.stringify(parsed).length > offloadConfig.threshold) {
              offloaded = await maybeOffloadRows(parsed, toolName, offloadConfig);
              if (offloaded) {
                log('large-result', 'OFFLOAD', `tool=${toolName} strategy=${strategy} path=${offloaded.filePath}`);
                return {
                  ...result,
                  content: result.content.map(c =>
                    c === item ? { type: 'text', text: offloaded.summary } : c
                  ),
                };
              }
            }
          }
        } catch { /* not JSON, skip */ }
      }
    }

    // Fallback: text offload
    offloaded = await maybeOffloadText(textContent, toolName, offloadConfig);

    if (!offloaded) return result;

    log('large-result', 'OFFLOAD', `tool=${toolName} strategy=${strategy} path=${offloaded.filePath}`);
    const nonTextItems = result.content.filter(c => c.type !== 'text');
    return {
      ...result,
      content: [...nonTextItems, { type: 'text', text: offloaded.summary }],
    };
  };
}
