// Every `"tester": "<name>"` in a shipped system.json must resolve to a probe
// some module actually exports. The Python validator cannot check this (the
// export is JS), so this is the gate — the same division of labour as
// tool-declaration.test.js, which executes register() for the same reason.
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { registerConfigTests, normalizeTester } from '../config-tests.js';

const CORE = path.resolve(import.meta.dirname, '../../modules');

function shippedModules() {
  return fs.readdirSync(CORE, { withFileTypes: true })
    .filter((e) => e.isDirectory() && !e.name.startsWith('_'))
    .map((e) => ({ name: e.name, _path: path.join(CORE, e.name) }))
    .filter((m) => fs.existsSync(path.join(m._path, 'module.json')));
}

function namedDeclarations() {
  const out = [];
  for (const mod of shippedModules()) {
    const systemPath = path.join(mod._path, 'system.json');
    if (!fs.existsSync(systemPath)) continue;
    let system;
    try {
      system = JSON.parse(fs.readFileSync(systemPath, 'utf-8'));
    } catch {
      continue;
    }
    if (!system || typeof system !== 'object' || Array.isArray(system)) continue;
    for (const [field, schema] of Object.entries(system)) {
      const spec = normalizeTester(schema?.tester);
      if (spec && spec.kind === 'toolbox') {
        out.push({ module: mod.name, field, name: spec.name });
      }
    }
  }
  return out;
}

describe('named config-test declarations', () => {
  it('every named probe is registered by the module that declares it', async () => {
    const registry = await registerConfigTests(shippedModules());
    const problems = [];
    for (const d of namedDeclarations()) {
      const entry = registry.get(d.name);
      if (!entry) problems.push(`${d.module}/${d.field}: no probe named '${d.name}'`);
      else if (entry.module !== d.module) {
        problems.push(`${d.module}/${d.field}: '${d.name}' is exported by '${entry.module}'`);
      }
    }
    expect(problems).toEqual([]);
  });

  it('every registered probe declares only its own module\'s paths', async () => {
    const registry = await registerConfigTests(shippedModules());
    const problems = [];
    for (const [name, entry] of registry) {
      for (const p of entry.paths) {
        if (!p.startsWith(`${entry.module}/`)) problems.push(`${name}: foreign path '${p}'`);
      }
    }
    expect(problems).toEqual([]);
  });

  it('no two modules export a probe under the same name', async () => {
    // A clashing name now fails CLOSED — both declarations are discarded and the
    // field answers DUPLICATE_TESTER — so a clash disables both probes rather
    // than pointing one module's button at another's. Still a defect to catch here.
    const seen = new Map();
    const clashes = [];
    const unimportable = [];
    for (const mod of shippedModules()) {
      const dir = path.join(mod._path, 'toolbox');
      if (!fs.existsSync(dir)) continue;
      for (const file of fs.readdirSync(dir).filter((f) => f.endsWith('.js'))) {
        let imported;
        try {
          imported = await import(path.join(dir, file));
        } catch {
          // Importing express does not resolve under vitest — the constraint
          // tool-declaration.test.js documents and works around the same way.
          // The skipped set is ASSERTED below, so a new unimportable file fails
          // here instead of quietly hiding a name clash inside it.
          unimportable.push(`${mod.name}/${file}`);
          continue;
        }
        for (const entry of imported.configTests || []) {
          if (!entry?.name) continue;
          if (seen.has(entry.name)) clashes.push(`${entry.name}: ${seen.get(entry.name)} and ${mod.name}`);
          seen.set(entry.name, mod.name);
        }
      }
    }
    expect(clashes).toEqual([]);
    // express (the api.js files) and nodemailer (core/email*.js) are the two
    // dependencies that do not resolve under vitest — the same two
    // tool-declaration.test.js documents. None of them exports `configTests`.
    expect(unimportable.sort()).toEqual([
      'bitbucket/api.js',
      'core/email-transport.js',
      'core/email.js',
      'github/api.js',
      'jira/api.js',
      'outlook/api.js',
    ]);
  });
});
