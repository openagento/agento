import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// A fresh modules root outside the source tree, unique per call (safe under parallel runs).
export const tempModulesDir = (prefix) => fs.mkdtempSync(path.join(os.tmpdir(), `agento-${prefix}-`));

// Write one module under `root`: `files` maps a path inside the module to its content
// (a string, or an object written as JSON). Returns the module dir.
export function writeModule(root, name, files) {
  const dir = path.join(root, name);
  for (const [rel, content] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
    fs.writeFileSync(path.join(dir, rel), typeof content === 'string' ? content : JSON.stringify(content));
  }
  return dir;
}
