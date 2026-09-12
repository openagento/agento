import { spawn } from 'node:child_process';
import { GitFailure } from './errors.js';

// The process environment is NOT inherited beyond PATH: no ambient identity, no
// credential helper, no GIT_* leakage from the toolbox into the child.
const BASE_ENV = {
  PATH: process.env.PATH,
  HOME: '/nonexistent',
  GIT_CONFIG_GLOBAL: '/dev/null',
  GIT_CONFIG_SYSTEM: '/dev/null',
  GIT_TERMINAL_PROMPT: '0',
  GIT_ALLOW_PROTOCOL: 'none',
  GIT_AUTHOR_NAME: 'Agento', GIT_AUTHOR_EMAIL: 'agento@localhost',
  GIT_COMMITTER_NAME: 'Agento', GIT_COMMITTER_EMAIL: 'agento@localhost',
};

const STDERR_CAP = 8 * 1024;
const DEFAULT_TIMEOUT_MS = 120_000;

/**
 * Run one git command.
 *
 * `maxBytes` is a fail-closed ceiling (overflow throws); `truncateAt` is a bounded
 * capture (stop at N bytes, stop the child, resolve with `truncated: true`). They are
 * two ends of the same knob and passing both is a programming error.
 *
 * spawn — not execFile — because execFile's options have no `input` field (that is the
 * sync API), so a command reading stdin would block until the timeout killed it.
 */
export async function runGit(argv, options = {}) {
  const { cwd, input, env, timeoutMs = DEFAULT_TIMEOUT_MS, maxBytes, truncateAt, keepStdinOpen = false } = options;
  if (maxBytes != null && truncateAt != null) {
    throw new Error('runGit: maxBytes and truncateAt are mutually exclusive');
  }
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn('git', argv, {
        cwd,
        env: { ...BASE_ENV, ...(env || {}) },
        stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch (err) {
      // Neither argv NOR the spawn error's own message: Node QUOTES the offending
      // argument ("The argument 'args[9]' must be a string without null bytes.
      // Received 'SECRET…'"), which put the agent's change message straight back
      // into the operator log by a second route after argv was removed from
      // GitFailure. The error CODE is a fixed identifier from a closed set, so it
      // says what went wrong without quoting a value — it goes in `code`, the one
      // GitFailure field an operator record may hold besides the exit status.
      const code = String(err?.code || err?.name || 'spawn failed');
      reject(new GitFailure(-1, code, code));
      return;
    }

    const chunks = [];
    let stdoutBytes = 0;
    let stderr = '';
    let truncated = false;
    let settled = false;
    let overflow = false;
    let timedOut = false;

    const kill = () => { try { child.kill('SIGKILL'); } catch { /* already gone */ } };

    const timer = setTimeout(() => { timedOut = true; kill(); }, timeoutMs);

    const fail = (exitCode, detail, code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new GitFailure(exitCode, detail, code));
    };
    const done = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };

    child.stdout.on('data', (chunk) => {
      if (truncated || overflow) return;
      if (maxBytes != null && stdoutBytes + chunk.length > maxBytes) {
        overflow = true;
        kill();
        return;
      }
      // `>` not `>=`: output that is EXACTLY the limit is complete, and the contract
      // (docs/modules/versioned-artifacts.md) says diffs PAST the limit are truncated.
      // `>=` reported a whole diff of exactly max_diff_bytes as truncated.
      if (truncateAt != null && stdoutBytes + chunk.length > truncateAt) {
        chunks.push(chunk.subarray(0, truncateAt - stdoutBytes));
        stdoutBytes = truncateAt;
        truncated = true;
        kill();
        return;
      }
      chunks.push(chunk);
      stdoutBytes += chunk.length;
    });

    child.stderr.on('data', (chunk) => {
      if (stderr.length >= STDERR_CAP) return;
      stderr += chunk.toString('utf8').slice(0, STDERR_CAP - stderr.length);
    });

    // Same reason as the spawn-throw path above: a spawn-class error message quotes
    // the offending argument, so only the code ("EACCES", "ENOENT") crosses into the
    // failure — as `code`, which IS logged. `stderr` is not, on any path.
    child.on('error', (err) => {
      const code = String(err?.code || err?.name || 'spawn failed');
      fail(-1, code, code);
    });

    child.on('close', (code, signal) => {
      // A SIGKILL we sent ourselves after a deliberate cutoff is not a failure —
      // that is the whole point of the truncateAt mode.
      if (truncated) { done({ stdout: Buffer.concat(chunks), stderr, truncated: true }); return; }
      if (overflow) { fail(code ?? -1, `output exceeded ${maxBytes} bytes`); return; }
      if (timedOut) { fail(code ?? -1, `timed out after ${timeoutMs}ms`); return; }
      if (code === 0) { done({ stdout: Buffer.concat(chunks), stderr, truncated: false }); return; }
      fail(code ?? -1, stderr || `terminated by ${signal}`);
    });

    child.stdin.on('error', () => { /* EPIPE when the child exits before reading */ });
    if (input != null) child.stdin.write(input);
    if (!keepStdinOpen) child.stdin.end();
  });
}
