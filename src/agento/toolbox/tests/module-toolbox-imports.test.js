import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

// Every .js file under a module's toolbox/, with its path relative to the module root.
function moduleToolboxFiles() {
  const out = [];
  for (const root of [path.join(here, '..', '..', 'modules'), path.join(here, '..', '..', '..', '..', 'app', 'code')]) {
    if (!fs.existsSync(root)) continue;
    for (const mod of fs.readdirSync(root)) {
      const modDir = path.join(root, mod);
      const dir = path.join(modDir, 'toolbox');
      if (!fs.existsSync(dir)) continue;
      const walk = (d) => {
        for (const name of fs.readdirSync(d, { withFileTypes: true })) {
          const full = path.join(d, name.name);
          if (name.isDirectory()) walk(full);
          else if (name.name.endsWith('.js')) out.push({ mod, modDir, full, src: fs.readFileSync(full, 'utf8') });
        }
      };
      walk(dir);
    }
  }
  return out;
}

// Structural guard for the class "a module toolbox file reaches outside its own module tree by a
// relative path". The repo checkout puts src/agento/modules/<m>/toolbox next to src/agento/toolbox,
// so '../../../toolbox/x.js' resolves there and every unit test passes — but the CONTAINER mounts
// the two trees at unrelated paths (/app/modules/core/<m>/toolbox vs the toolbox package root), so
// the same specifier resolves to nothing, the file fails to load, and the module registers no tools
// and no REST routes at all. Framework code must reach a module through the registration context.
describe('module toolbox imports', () => {
  it('no module toolbox file imports outside its own module directory', () => {
    const offenders = [];
    for (const { mod, modDir, full, src } of moduleToolboxFiles()) {
      const specifiers = [
        ...src.matchAll(/(?:^|\n)\s*(?:import|export)[\s\S]*?from\s*['"]([^'"]+)['"]/g),
        // A side-effect import has no `from`, and it fails to load exactly the same way.
        ...src.matchAll(/(?:^|\n)\s*import\s*['"]([^'"]+)['"]/g),
        ...src.matchAll(/\bimport\(\s*['"]([^'"]+)['"]\s*\)/g),
      ].map((m) => m[1]);
      for (const spec of specifiers) {
        if (!spec.startsWith('.')) continue; // a bare specifier is a node_modules package
        const target = path.resolve(path.dirname(full), spec);
        if (!target.startsWith(modDir + path.sep)) {
          offenders.push(`${mod}/${path.relative(modDir, full)}: ${spec}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it('finds the module files it claims to scan', () => {
    expect(moduleToolboxFiles().length).toBeGreaterThan(5);
  });
});
