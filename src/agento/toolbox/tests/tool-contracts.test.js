import { describe, it, expect, vi } from 'vitest';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { normalizeObjectSchema, objectFromShape } from '@modelcontextprotocol/sdk/server/zod-compat.js';
import { toJsonSchemaCompat } from '@modelcontextprotocol/sdk/server/zod-json-schema-compat.js';

// email.js -> email-transport.js -> nodemailer, which does not resolve under vitest (tool-declaration.test.js).
vi.mock('../../modules/core/toolbox/email-transport.js', () => ({
  createTransporter: () => ({ sendMail: vi.fn() }),
}));

import { MODULES, REGISTRARS, recordRegistrations } from './shipped-registrars.js';

// The lock a module ships beside module.json: {"<tool>": "<contract>"}. The framework's tool
// catalog reads it into contract_hash, so a schema change sends the tool back to admin review.
const LOCK_FILE = 'tool-contracts.json';
const UPDATE = process.env.CONTRACTS_UPDATE === '1';
const SWITCH = 'switch';
const PLAYWRIGHT_MCP = JSON.parse(fs.readFileSync(
  fileURLToPath(new URL('../node_modules/@playwright/mcp/package.json', import.meta.url)), 'utf8'));
const PASSTHROUGH = `@playwright/mcp@${PLAYWRIGHT_MCP.version}`;

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

// The same conversion the MCP SDK applies when it answers tools/list.
export function schemaContract(shape) {
  const noParameters = !shape._def && !shape._zod && Object.keys(shape).length === 0;
  const object = normalizeObjectSchema(noParameters ? objectFromShape(shape) : shape);
  const json = toJsonSchemaCompat(object, { strictUnions: true, pipeStrategy: 'input' });
  return createHash('sha1').update(canonical(json)).digest('hex');
}

function manifestTools(moduleName) {
  const manifest = JSON.parse(fs.readFileSync(path.join(MODULES, moduleName, 'module.json'), 'utf8'));
  return manifest.tools || [];
}

// Static tools by their schema; a name only the upstream passthrough registers by the pinned
// upstream package; a toolset master switch (a `requires` target that registers nothing) by SWITCH.
async function currentContracts(moduleName) {
  const tools = manifestTools(moduleName);
  const declared = tools.map(t => t.name);
  const switches = new Set(tools.map(t => t.requires).filter(Boolean));
  const asUpstream = declared.filter(name => !switches.has(name))
    .map(name => ({ name, description: name, inputSchema: { type: 'object', properties: {} } }));
  const staticTools = new Map((await recordRegistrations(moduleName, [])).map(r => [r.name, r.schema]));
  const withUpstream = new Set((await recordRegistrations(moduleName, asUpstream)).map(r => r.name));
  const contracts = {};
  const unrecorded = [];
  for (const name of [...declared].sort()) {
    if (staticTools.has(name)) contracts[name] = schemaContract(staticTools.get(name));
    else if (withUpstream.has(name)) contracts[name] = PASSTHROUGH;
    else if (switches.has(name)) contracts[name] = SWITCH;
    else unrecorded.push(name);
  }
  return { contracts, unrecorded };
}

function readLock(moduleName) {
  const file = path.join(MODULES, moduleName, LOCK_FILE);
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
}

describe('tool contract locks', () => {
  it.each(Object.keys(REGISTRARS))('%s: every declared tool has a current contract', async (moduleName) => {
    const { contracts, unrecorded } = await currentContracts(moduleName);
    expect(unrecorded).toEqual([]);
    if (UPDATE) {
      fs.writeFileSync(path.join(MODULES, moduleName, LOCK_FILE), `${JSON.stringify(contracts, null, 2)}\n`);
      return;
    }
    expect(readLock(moduleName), `${moduleName}/${LOCK_FILE} is stale: run npm run contracts:update`).toEqual(contracts);
  });

  it('every in-tree module with tools has a registrar here', () => {
    const withTools = fs.readdirSync(MODULES)
      .filter(name => fs.existsSync(path.join(MODULES, name, 'module.json')) && manifestTools(name).length);
    expect(withTools.sort()).toEqual(Object.keys(REGISTRARS).sort());
  });

  it('a schema change changes the contract', async () => {
    const { z } = await import('zod');
    expect(schemaContract({ a: z.string() })).not.toBe(schemaContract({ a: z.number() }));
  });
});
