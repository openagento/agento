import { z } from 'zod';
import { readFile, writeFile, mkdir, copyFile, unlink } from 'fs/promises';
import { resolve } from 'path';

// --- Session cookie injection ---
let sessionCookies = [];
try {
  const raw = await readFile('/app/session.json', 'utf8');
  sessionCookies = JSON.parse(raw).cookies || [];
} catch {
  // No session.json or no cookies — that's fine
}

function sessionCookiesForUrl(urlString) {
  let hostname;
  try { hostname = new URL(urlString).hostname.toLowerCase(); } catch { return []; }
  return sessionCookies.filter(c => {
    const domain = (c.domain || '').replace(/^\./, '').toLowerCase();
    return hostname === domain || hostname.endsWith('.' + domain);
  });
}

function buildCookieJs(cookies) {
  return cookies.map(c => {
    let str = `${c.name}=${encodeURIComponent(c.value)}; path=${c.path || '/'}`;
    if (c.domain) str += `; domain=${c.domain}`;
    str += `; max-age=31536000; SameSite=${c.sameSite || 'Lax'}`;
    return `document.cookie=${JSON.stringify(str)}`;
  }).join('; ');
}

// --- Helpers (pure functions, no config dependency) ---

function parseList(value) {
  if (!value) return [];
  return value.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
}

function parseBool(value, defaultValue) {
  if (value === undefined || value === null || value === '') return defaultValue;
  return value === true || value === 'true' || value === '1';
}

// --- JSON Schema → Zod converter (for upstream Playwright tool passthrough) ---

function jsonSchemaPropertyToZod(prop) {
  if (prop.enum) return z.enum(prop.enum);
  switch (prop.type) {
    case 'string':  return z.string();
    case 'number':  return z.number();
    case 'integer': return z.number().int();
    case 'boolean': return z.boolean();
    case 'array':   return z.array(prop.items ? jsonSchemaPropertyToZod(prop.items) : z.unknown());
    case 'object':  return prop.properties
      ? z.object(Object.fromEntries(Object.entries(prop.properties).map(([k, v]) => [k, jsonSchemaPropertyToZod(v)])))
      : z.record(z.unknown());
    default: throw new Error(`Unsupported JSON Schema type: ${prop.type}`);
  }
}

function jsonSchemaToZodShape(inputSchema) {
  const shape = {
    user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
  };
  const props = inputSchema?.properties || {};
  const required = new Set(inputSchema?.required || []);
  for (const [key, prop] of Object.entries(props)) {
    let zodType = jsonSchemaPropertyToZod(prop);
    if (prop.description) zodType = zodType.describe(prop.description);
    if (!required.has(key)) zodType = zodType.optional();
    shape[key] = zodType;
  }
  return shape;
}

// --- Tool definitions (static schemas for known Playwright MCP tools) ---

const BROWSER_TOOLS = {
  browser_navigate: {
    description: [
      'Navigate to a URL in the browser.',
      'Only whitelisted domains and HTTPS (by default) are allowed.',
      'Session cookies from session.json are automatically injected on first visit to a domain.',
      'Returns an accessibility snapshot of the page.',
    ].join('\n'),
    schema: {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      url: z.string().url().describe('URL to navigate to'),
    },
    urlParam: 'url',
    playwrightName: 'browser_navigate',
  },
  browser_wait_for: {
    description: 'Wait for a specified amount of time.',
    schema: {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      time: z.number().int().positive().describe('Time to wait in milliseconds'),
    },
    urlParam: null,
  },
  browser_take_screenshot: {
    description: [
      'Take a screenshot of the current page.',
      'Returns a PNG image. Navigate to a page first.',
      'The screenshot is also saved to the artifacts directory under screenshots/{job_id}/{filename}.',
      'Pass job_id from your execution context (SOUL.md) to organise the file correctly.',
    ].join('\n'),
    schema: {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      job_id: z.string().optional().describe('Job ID from SOUL.md — used to organise the screenshot folder'),
      filename: z.string().optional().describe('PNG filename. Defaults to {timestamp}.png. Use a fixed name to overwrite on each run.'),
    },
    urlParam: null,
  },
  browser_snapshot: {
    description: [
      'Capture an accessibility snapshot of the current page.',
      'Returns the page structure as text. Navigate to a page first.',
    ].join('\n'),
    schema: {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
    },
    urlParam: null,
  },
  browser_evaluate: {
    description: [
      'Evaluate a JavaScript arrow function in the current page and return the result.',
      'The function parameter must be an arrow function, e.g. "() => document.title".',
    ].join('\n'),
    schema: {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      function: z.string().describe('Arrow function to evaluate, e.g. "() => document.title"'),
    },
    urlParam: null,
  },
  browser_start_video: {
    description: [
      'Start recording a video of the browser page.',
      'Pass width and height to set the video resolution (defaults to viewport size).',
      'Stop recording with browser_stop_video when done.',
    ].join('\n'),
    schema: {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      width: z.number().int().positive().optional().describe('Video width in pixels'),
      height: z.number().int().positive().optional().describe('Video height in pixels'),
    },
    urlParam: null,
  },
  browser_stop_video: {
    description: [
      'Stop recording and save the video.',
      'The video file is saved to the artifacts directory under videos/{job_id}/{filename}.',
      'Pass job_id from your execution context (SOUL.md) to organise the file correctly.',
    ].join('\n'),
    schema: {
      user: z.string().email().describe('Your (the LLM agent) email address from SOUL.md — identity credential'),
      job_id: z.string().optional().describe('Job ID from SOUL.md — used to organise the video folder'),
      filename: z.string().optional().describe('WebM filename. Defaults to {timestamp}.webm.'),
    },
    urlParam: null,
  },
};

// --- Registration ---

const HEALTHCHECK_LISTTOOLS_TIMEOUT_MS = 3000;

function describePlaywrightState(getState) {
  const s = typeof getState === 'function' ? getState() : null;
  if (!s) return { error: 'Playwright MCP not connected' };
  if (s.state === 'starting') return { error: 'Browser service is starting up — try again in a few seconds.' };
  if (s.state === 'restarting') return { error: `Browser service is restarting (attempt ${s.attempt} of ${s.maxAttempts}) — try again in a moment.` };
  if (s.state === 'failed') {
    const last = s.lastError ? `: ${s.lastError}` : '';
    return { error: `Browser service has failed permanently after ${s.attempt} restart attempts${last}. Check toolbox logs.` };
  }
  return { error: 'Browser service is in inconsistent state — check toolbox logs.' };
}

export async function healthcheck({ playwright }) {
  const client = playwright.getClient();
  if (!client) {
    const { error } = describePlaywrightState(playwright.getState);
    return [{ tool: 'browser', status: 'fail', error }];
  }
  const t0 = Date.now();
  try {
    await Promise.race([
      client.listTools(),
      new Promise((_, reject) => setTimeout(() => reject(new Error(`listTools timeout after ${HEALTHCHECK_LISTTOOLS_TIMEOUT_MS}ms`)), HEALTHCHECK_LISTTOOLS_TIMEOUT_MS)),
    ]);
    return [{ tool: 'browser', status: 'ok', ms: Date.now() - t0 }];
  } catch (err) {
    return [{ tool: 'browser', status: 'fail', error: err.message }];
  }
}

export function register(server, { log, playwright, moduleConfigs, isToolEnabled, artifactsDir }) {
  const cfg = moduleConfigs?.core || {};
  // Per-SESSION, not module-level: register() runs once per MCP session, so a module-level Set
  // would accumulate for the process lifetime and let one agent_view's cookie injection affect
  // another's. (`sessionCookies` above stays module-level — it is read-only, loaded once at import.)
  const injectedDomains = new Set();
  // At startup (registerModuleRestApis) isToolEnabled is undefined and the server is a stub.
  // Every browser tool is declared in core/module.json with requires: "browser", so this one
  // helper is the whole enablement gate — no module-level early-return, no separate allow-list.
  const enabled = (name) => !isToolEnabled || isToolEnabled(name);
  const allowedDomains = parseList(cfg.allowed_domains);
  const allowSubdomains = parseBool(cfg.allow_subdomains, true);
  const allowHttp = parseBool(cfg.allow_http, false);
  const registeredPassthroughNames = [];

  function validateDomain(urlString) {
    let parsed;
    try {
      parsed = new URL(urlString);
    } catch {
      return { allowed: false, reason: `Invalid URL: "${urlString}"` };
    }

    if (!allowHttp && parsed.protocol === 'http:') {
      return { allowed: false, reason: `HTTP not allowed. Use HTTPS.` };
    }

    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return { allowed: false, reason: `Protocol "${parsed.protocol}" not allowed.` };
    }

    if (allowedDomains.length === 0) {
      return { allowed: false, reason: 'No domains configured (CONFIG__CORE__ALLOWED_DOMAINS).' };
    }

    const hostname = parsed.hostname.toLowerCase();
    const match = allowedDomains.some(domain => {
      if (hostname === domain) return true;
      if (allowSubdomains && hostname.endsWith('.' + domain)) return true;
      return false;
    });

    if (!match) {
      return { allowed: false, reason: `Domain "${hostname}" not in allowed list.` };
    }

    return { allowed: true };
  }

  if (sessionCookies.length) {
    log('browser', 'SESSION', `Loaded ${sessionCookies.length} session cookies for injection`);
  }

  log('browser', 'INIT', `domains=[${allowedDomains.join(',')}] subdomains=${allowSubdomains} http=${allowHttp}`);

  for (const [name, def] of Object.entries(BROWSER_TOOLS)) {
    if (!enabled(name)) continue;

    server.tool(
      name,
      def.description,
      def.schema,
      async (args) => {
        // Strip our params — they're not Playwright MCP's
        const { user, job_id, filename, width, height, ...toolArgs } = args;

        // Domain validation for URL-bearing tools
        if (def.urlParam && args[def.urlParam]) {
          const result = validateDomain(args[def.urlParam]);
          if (!result.allowed) {
            log(name, 'BLOCKED', `user=${user} url="${args[def.urlParam]}" — ${result.reason}`);
            return {
              content: [{ type: 'text', text: `Error: ${result.reason} Allowed domains: ${allowedDomains.join(', ')}` }],
              isError: true,
            };
          }
        }

        const client = playwright.getClient();
        if (!client) {
          const { error } = describePlaywrightState(playwright.getState);
          log(name, 'ERROR', `user=${user} — ${error}`);
          return {
            content: [{ type: 'text', text: `Error: ${error}` }],
            isError: true,
          };
        }

        try {
          // Pre-navigation cookie injection
          if (name === 'browser_navigate' && toolArgs.url) {
            const preCookies = sessionCookiesForUrl(toolArgs.url);
            let preHostname;
            try { preHostname = new URL(toolArgs.url).hostname; } catch { /* invalid url handled below */ }
            if (preCookies.length && preHostname && !injectedDomains.has(preHostname)) {
              injectedDomains.add(preHostname);
              const parsed = new URL(toolArgs.url);
              const lightUrl = `${parsed.protocol}//${parsed.host}/robots.txt`;
              log(name, 'PRE-NAV', `user=${user} navigating to ${lightUrl} for cookie setup`);
              await client.callTool({ name: 'browser_navigate', arguments: { url: lightUrl } });
              const cookieJs = buildCookieJs(preCookies);
              await client.callTool({ name: 'browser_evaluate', arguments: { function: `() => { ${cookieJs} }` } });
              const ck = await client.callTool({ name: 'browser_evaluate', arguments: { function: '() => document.cookie' } });
              log(name, 'COOKIES', `user=${user} injected ${preCookies.length} cookie(s) for ${preHostname}: ${(ck?.content?.[0]?.text || '').substring(0, 200)}`);
            }
          }

          // browser_wait_for: plain Node.js sleep — no Playwright MCP call needed
          if (name === 'browser_wait_for') {
            const ms = Math.min(toolArgs.time ?? 1000, 30000);
            await new Promise(r => setTimeout(r, ms));
            log(name, 'OK', `user=${user} waited ${ms}ms`);
            return { content: [{ type: 'text', text: `Waited ${ms}ms` }] };
          }

          // browser_start_video: pick the recording size. Without an explicit
          // size @playwright/mcp falls back to a hardcoded 800x600 canvas and
          // pads the differently-sized page frame into the corner — low-res and
          // letterboxed. Default to the browser viewport so the recording is
          // full-frame and crisp; explicit width/height still override, and a
          // single given dimension is completed from the viewport.
          if (name === 'browser_start_video') {
            const viewport = typeof playwright.getViewport === 'function' ? (playwright.getViewport() || {}) : {};
            const vw = width || viewport.width;
            const vh = height || viewport.height;
            const videoArgs = (vw && vh) ? { size: { width: vw, height: vh } } : {};
            log(name, 'FORWARD', `user=${user} args=${JSON.stringify(videoArgs)}`);
            const result = await client.callTool({ name: 'browser_start_video', arguments: videoArgs });
            if (result.isError) {
              log(name, 'PW-ERROR', `user=${user} ${(result.content?.[0]?.text || '').substring(0, 200)}`);
            } else {
              log(name, 'OK', `user=${user} recording started`);
            }
            return result;
          }

          // browser_stop_video: stop recording, find saved file(s), move to organized path
          if (name === 'browser_stop_video') {
            log(name, 'FORWARD', `user=${user} stopping video recording`);
            const result = await client.callTool({ name: 'browser_stop_video', arguments: {} });
            if (result.isError) {
              log(name, 'PW-ERROR', `user=${user} ${(result.content?.[0]?.text || '').substring(0, 200)}`);
              return result;
            }

            const text = result.content?.find(c => c.type === 'text')?.text || '';
            const pathMatches = [...text.matchAll(/\[Video[^\]]*\]\(([^)]+\.webm)\)/g)];

            if (pathMatches.length === 0) {
              log(name, 'OK', `user=${user} no video files found in response`);
              return result;
            }

            const savedPaths = [];
            for (const [i, match] of pathMatches.entries()) {
              const srcPath = resolve(match[1]);
              const suffix = pathMatches.length > 1 ? `-${i}` : '';
              const fname = filename
                ? filename.replace(/\.webm$/i, '') + suffix + '.webm'
                : `${Date.now()}${suffix}.webm`;
              const folder = job_id
                ? `${artifactsDir}/videos/${job_id}`
                : `${artifactsDir}/videos`;
              const destPath = `${folder}/${fname}`;

              try {
                await mkdir(folder, { recursive: true });
                await copyFile(srcPath, destPath);
                await unlink(srcPath).catch(() => {});
                savedPaths.push(destPath);
                log(name, 'SAVED', `user=${user} ${srcPath} → ${destPath}`);
              } catch (moveErr) {
                log(name, 'WARN', `user=${user} failed to move video: ${moveErr.message}`);
                savedPaths.push(srcPath);
              }
            }

            log(name, 'OK', `user=${user} ${savedPaths.length} video(s) saved`);
            return {
              content: [
                { type: 'text', text: savedPaths.map(p => `Video saved to: ${p}`).join('\n') },
              ],
            };
          }

          log(name, 'FORWARD', `user=${user} args=${JSON.stringify(toolArgs)}`);
          let result = await client.callTool({ name: def.playwrightName || name, arguments: toolArgs });
          if (result.isError) {
            const errText = result.content?.[0]?.text || 'unknown error';
            log(name, 'PW-ERROR', `user=${user} ${errText.substring(0, 200)}`);
          } else {
            log(name, 'OK', `user=${user} contentItems=${result.content?.length || 0}`);
          }

          // For browser_take_screenshot: save PNG to disk and append the path as a text content item
          if (name === 'browser_take_screenshot' && !result.isError) {
            const imageItem = result.content?.find(c => c.type === 'image' && c.data);
            if (imageItem) {
              const fname = filename || `${Date.now()}.png`;
              const folder = job_id
                ? `${artifactsDir}/screenshots/${job_id}`
                : `${artifactsDir}/screenshots`;
              const filePath = `${folder}/${fname}`;
              try {
                await mkdir(folder, { recursive: true });
                await writeFile(filePath, Buffer.from(imageItem.data, 'base64'));
                log(name, 'SAVED', `user=${user} path=${filePath}`);
                result.content = [
                  ...result.content,
                  { type: 'text', text: `Screenshot saved to: ${filePath}` },
                ];
              } catch (saveErr) {
                log(name, 'WARN', `user=${user} failed to save screenshot: ${saveErr.message}`);
              }
            }
          }

          return result;
        } catch (err) {
          log(name, 'ERROR', `user=${user} ${err.message}`);
          return {
            content: [{ type: 'text', text: `Browser error: ${err.message}` }],
            isError: true,
          };
        }
      },
    );
  }

  // --- Passthrough registration for upstream Playwright tools ---
  const upstreamTools = playwright.getTools();
  for (const tool of upstreamTools) {
    if (BROWSER_TOOLS[tool.name]) continue; // custom wrapper takes priority
    // An upstream tool that core/module.json does not declare has no is_enabled key, so the
    // gate denies it and it is never registered — new upstream tools are fail-closed.
    if (!enabled(tool.name)) continue;

    let zodShape;
    try {
      zodShape = jsonSchemaToZodShape(tool.inputSchema);
    } catch (err) {
      log(tool.name, 'SKIP', `Failed to convert schema: ${err.message}`);
      continue;
    }

    server.tool(tool.name, tool.description || '', zodShape, async (args) => {
      const { user, ...toolArgs } = args;
      const client = playwright.getClient();
      if (!client) {
        const { error } = describePlaywrightState(playwright.getState);
        log(tool.name, 'ERROR', `user=${user} — ${error}`);
        return {
          content: [{ type: 'text', text: `Error: ${error}` }],
          isError: true,
        };
      }
      try {
        log(tool.name, 'FORWARD', `user=${user} args=${JSON.stringify(toolArgs)}`);
        const result = await client.callTool({ name: tool.name, arguments: toolArgs });
        if (result.isError) {
          log(tool.name, 'PW-ERROR', `user=${user} ${(result.content?.[0]?.text || '').substring(0, 200)}`);
        } else {
          log(tool.name, 'OK', `user=${user} contentItems=${result.content?.length || 0}`);
        }
        return result;
      } catch (err) {
        log(tool.name, 'ERROR', `user=${user} ${err.message}`);
        return {
          content: [{ type: 'text', text: `Browser error: ${err.message}` }],
          isError: true,
        };
      }
    });

    registeredPassthroughNames.push(tool.name);
  }

  if (registeredPassthroughNames.length) {
    log('browser', 'INIT', `Passthrough tools registered: ${registeredPassthroughNames.join(', ')}`);
  }
}
