export const ERROR_CODES = Object.freeze({
  ARTIFACT_NOT_FOUND: 'ARTIFACT_NOT_FOUND', ARTIFACT_ACCESS_DENIED: 'ARTIFACT_ACCESS_DENIED',
  // A NAME collision, distinct from a store failure: `init` retries under the next
  // number for an agent, and only the admin CLI ever sees it — an operator names a code
  // deliberately, so renaming theirs would publish at an address they did not choose.
  ARTIFACT_ALREADY_EXISTS: 'ARTIFACT_ALREADY_EXISTS',
  DRAFT_NOT_FOUND: 'DRAFT_NOT_FOUND', DRAFT_LOCKED: 'DRAFT_LOCKED',
  VERSION_NOT_FOUND: 'VERSION_NOT_FOUND', VERSION_ALREADY_EXISTS: 'VERSION_ALREADY_EXISTS',
  INVALID_PATH: 'INVALID_PATH', PATH_OUTSIDE_ARTIFACT: 'PATH_OUTSIDE_ARTIFACT',
  SYMLINK_NOT_ALLOWED: 'SYMLINK_NOT_ALLOWED', FILE_TOO_LARGE: 'FILE_TOO_LARGE',
  ARTIFACT_TOO_LARGE: 'ARTIFACT_TOO_LARGE', TOO_MANY_FILES: 'TOO_MANY_FILES',
  // A CLIENT error, deliberately outside INTERNAL_CODES below: the caller reached a
  // quota, the store did not fail, and an operator does not need a log line per attempt.
  ARTIFACT_LIMIT_REACHED: 'ARTIFACT_LIMIT_REACHED',
  CURRENT_VERSION_CHANGED: 'CURRENT_VERSION_CHANGED', STORAGE_OPERATION_FAILED: 'STORAGE_OPERATION_FAILED',
  PUBLISH_FAILED: 'PUBLISH_FAILED', INVALID_OPERATION: 'INVALID_OPERATION',
  // Two distinct desk failures, because the remedies differ: WORKSPACE_UNAVAILABLE
  // means the session has no workspace at all and the agent can do nothing, while
  // DESK_MISSING means "materialize it first" and the agent can recover alone.
  WORKSPACE_UNAVAILABLE: 'WORKSPACE_UNAVAILABLE', DESK_MISSING: 'DESK_MISSING',
});

export class ArtifactError extends Error {
  // `options` is forwarded to Error so `{cause: gitFailure}` actually lands on
  // `.cause`. Without it the third argument would be silently dropped and the
  // GitFailure the backend attaches (Task 5) would never reach the service's
  // bounded log line — the failure would be untraceable in production.
  constructor(code, message, options) {
    super(`${code}: ${message}`, options);
    this.name = 'ArtifactError'; this.code = code; this.detail = message;
  }
}

/** A failed Git command, INTERNAL to the backend. It carries the exit status and
 *  its stderr, capped in size, so the backend can decide what the failure MEANS — a caller
 *  that turns every failure into "already exists" or "changed concurrently" will
 *  report a disk-full or permission error as a concurrency conflict.
 *
 *  A spawn-class failure carries a THIRD field, `code` — "ENOENT", "EACCES" — which
 *  IS logged, because a Node/OS error code is a fixed identifier from a closed set and
 *  cannot contain a caller's text. It is the one operator-facing fact about a failure
 *  that produced no git output at all: `git: not found` on a toolbox image that was
 *  never rebuilt reads as `GitFailure code=ENOENT exit=-1`.
 *
 *  IT DOES NOT CARRY THE ARGUMENT LIST, and that is a security property, not a
 *  style choice. Its `stderr` is INTERNAL for the same reason and is never logged —
 *  git echoes the pathspec it was given, so stderr carries caller text too; see
 *  `errorFacts`. `git commit -m <message>` puts an agent-supplied string in argv,
 *  so an error message built from argv carries agent input — with its newlines and
 *  NUL bytes — into every sink that ever formats this error, including the
 *  persistent toolbox log. One forged newline there is a forged log record. The
 *  operation's identity reaches the operator through the ArtifactError the caller raises
 *  ("the batch could not be saved"), which is the layer that actually knows it.
 *
 *  A GitFailure must never reach a tool response. THREE boundaries stand between,
 *  and they are not interchangeable:
 *    1. The backend's own mappings, which are the only ones with the CONTEXT to say
 *       what a failure meant (a failed `update-ref` after checking whether the ref
 *       now exists is VERSION_ALREADY_EXISTS, not a generic storage error).
 *    2. `audited` in service.js — a fallback for MUTATIONS ONLY, whose real job is
 *       the audit row's `error_code`. The reads write no audit row and never pass
 *       through it.
 *    3. `toToolError`, which turns anything that is not a ArtifactError into a generic
 *       STORAGE_OPERATION_FAILED response. This is the last net, and for a read it is
 *       the only one after (1). */
export class GitFailure extends Error {
  constructor(exitCode, stderr, code) {
    super(`git command failed (exit ${exitCode})`);
    this.name = 'GitFailure'; this.exitCode = exitCode; this.stderr = stderr;
    if (code != null) this.code = code;
  }
}

// The codes that mean "the store failed", as opposed to "you asked for something
// that is not there". Only these are operator events: VERSION_NOT_FOUND and
// DRAFT_NOT_FOUND carry a `cause` too, and logging every legitimate miss buries the
// one line that matters.
const INTERNAL_CODES = new Set([
  ERROR_CODES.STORAGE_OPERATION_FAILED, ERROR_CODES.PUBLISH_FAILED,
]);

/** ONE bounded line: the SIZE-and-shape half of the logging rule, applied last to
 *  everything this module writes.
 *    - `\s+` collapse: nothing becomes several log records.
 *    - control-character strip: `\s` does NOT match NUL, and a NUL (or an ESC
 *      sequence) reaching a log file is a record no operator can read or grep.
 *    - final slice: bounds the record whatever produced it.
 *  It is NOT the disclosure half and never was — that is `errorFacts`, which decides
 *  WHICH fields may appear at all. Bounding a string that should not be there yields
 *  a tidy leak; both halves are needed. */
export function boundedLine(text) {
  return String(text ?? '')
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 200);
}

/** THE ONE RULE THIS MODULE LOGS BY: a class and a machine code, never free-form text
 *  from anywhere. No message, and NO GIT STDERR.
 *
 *  The rule got here in three steps, and the last two are the interesting ones.
 *  `boundedLine` stopped a caller's text from FORGING a record, but not from being
 *  DISCLOSED. Dropping `err.message` closed the messages — a DB driver quotes the
 *  offending value back ("Incorrect string value … for column 'description'", and
 *  that description is agent-supplied), an fs error quotes the path. Git's stderr was
 *  kept one round longer on the argument that it is git's text rather than a caller's.
 *  THAT ARGUMENT WAS WRONG, and it was disproved by reproduction, not by reasoning:
 *  git ECHOES THE PATHSPEC it was given, so reading a file under a directory named
 *  "SECRET…\n[forged]" — a legal path; only NUL and ".." are refused — makes git say
 *  `fatal: …:SECRET…[forged]: bad file`, and that landed in the operator log. Any
 *  field git fills from argv carries whatever the caller put in argv.
 *
 *  So what an operator reads for a broken store is the mapped ArtifactError detail plus the
 *  class and the exit code: `STORAGE_OPERATION_FAILED: the artifact store is damaged —
 *  cause: GitFailure exit=128`. That is the deliberate cost of the rule. Deeper git
 *  output belongs behind an administrator-only diagnostic path, which this release
 *  does not have (see docs/modules/versioned-artifacts.md).
 *
 *  Every field is also VALIDATED rather than trusted, because a foreign error object
 *  carries whatever properties its thrower chose: a `name` of "GitFailure" must not
 *  unlock anything (the class is checked with `instanceof`), a `code` is accepted only
 *  if it looks like a machine code, and an exit status only if it is an integer. */
const MACHINE_CODE = /^[A-Za-z][A-Za-z0-9_]{0,40}$/;

export function errorFacts(err) {
  if (!err) return null;
  const isGit = err instanceof GitFailure;
  const name = isGit ? 'GitFailure'
    : (typeof err.name === 'string' && MACHINE_CODE.test(err.name) ? err.name : 'Error');
  const parts = [name];
  if (typeof err.code === 'string' && MACHINE_CODE.test(err.code)) parts.push(`code=${err.code}`);
  else if (Number.isInteger(err.code)) parts.push(`code=${err.code}`);
  if (isGit && Number.isInteger(err.exitCode)) parts.push(`exit=${err.exitCode}`);
  return boundedLine(parts.join(' '));
}

const causeLine = (err) => errorFacts(err?.cause);

// The ONLY thing an LLM ever sees on failure.
export function toToolError(err, log) {
  if (err instanceof ArtifactError) {
    // The cause exists so a disk, permission or object-store failure is traceable.
    // Carried and never read, it was only ballast: the response stays generic and
    // the operator log gets the diagnostic.
    if (INTERNAL_CODES.has(err.code)) {
      const detail = causeLine(err);
      // Bounded as a WHOLE, not only its cause: every record this module writes is
      // one bounded line, with no exception that a future detail string could grow into.
      log?.('versioned_artifacts', 'ERROR',
        boundedLine(`${err.code}: ${err.detail}${detail ? ` — cause: ${detail}` : ''}`));
    }
    return { error_code: err.code, message: err.detail };
  }
  // `context.log` is CALLABLE — `(tool, status, details)` (`src/agento/toolbox/log.js:16-27`,
  // wired at `server.js:26` and overridden per MCP session in `createServer`). It is NOT an
  // object with `.error`/`.info`. `log?.error?.(…)` would be a no-op that optional chaining
  // swallows in silence, which is the worst possible failure for a diagnostic path. The
  // house idiom is `log('<tool>', 'ERROR'|'WARN'|'OK', details)` — see jira-proxy.js:16.
  // A fixed description plus `errorFacts`. The message is NOT logged: this branch
  // exists for errors this module did not build, so its text is by definition
  // written by something else — which is how an agent's change message reached this
  // line once already, quoted back by Node's own spawn error.
  log?.('versioned_artifacts', 'ERROR', `internal error: ${errorFacts(err) ?? 'unknown'}`);
  return { error_code: ERROR_CODES.STORAGE_OPERATION_FAILED, message: 'Operation failed' };
}
