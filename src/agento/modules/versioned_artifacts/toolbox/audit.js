import { errorFacts } from './errors.js';
import path from 'node:path';
import { appendFile } from 'node:fs/promises';

/**
 * Acceptance criterion 24 requires every mutation to be auditable, so a failed
 * INSERT may not vanish — but neither may it fail a publish that already
 * succeeded. Write the row; if that throws, append it to a durable file.
 */
export async function recordAudit(pool, log, storageRoot, row) {
  // EVERY caller-supplied field that reaches a sink is bounded here, once, before
  // `record` exists — not in the parameter list of one of the two sinks, and not
  // only the two obvious ones. An identifier is a request argument too: the
  // service rejects a malformed one, but a mutation whose refusal happens BELOW
  // the audit boundary is audited with the raw value, and the fallback file takes
  // it verbatim. Each bound is its own SQL column's width, so what the file holds
  // is what the table would have held.
  const WIDTH = {
    artifactCode: 64, operation: 48, draftId: 64, versionId: 64, previousVersion: 64,
    revision: 40, actor: 255, result: 16, errorCode: 32, description: 255,
  };
  const cap = (v, n) => (v == null || v === '' ? null : String(v).slice(0, n));
  const record = { ts: new Date().toISOString(), ...row };
  for (const [field, width] of Object.entries(WIDTH)) record[field] = cap(row[field], width);
  try {
    if (!pool) throw new Error('no db pool');
    await pool.execute(
      `INSERT INTO versioned_artifact_audit
       (artifact_code, operation, draft_id, version_id, previous_version, revision,
        job_id, agent_view_id, actor, result, error_code, description)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      // The capped `record`, never the raw `row`: an oversized value would make the
      // INSERT itself throw and divert a perfectly writable row into the fallback file.
      [record.artifactCode, record.operation, record.draftId, record.versionId,
       record.previousVersion, record.revision, row.jobId ?? null,
       row.agentViewId ?? null, record.actor, record.result, record.errorCode,
       record.description],
    );
  } catch (err) {
    // The driver's MESSAGE is not logged: it quotes the statement's parameters back,
    // and one of those is the agent-supplied `description`. `errorFacts` keeps the
    // class and the machine code (ER_DATA_TOO_LONG, ECONNREFUSED), which is what an
    // operator acts on.
    log?.('versioned_artifacts', 'ERROR', `audit insert failed: ${errorFacts(err) ?? 'unknown'}`);
    try {
      await appendFile(path.join(storageRoot, 'audit-fallback.log'), JSON.stringify(record) + '\n');
    } catch (err2) {
      log?.('versioned_artifacts', 'ERROR',
        `audit fallback ALSO failed, event unrecorded: ${errorFacts(err2) ?? 'unknown'}`);
    }
  }
}
