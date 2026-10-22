-- A thread groups the jobs of one conversation of work (a Jira issue, a mail conversation,
-- a PR, a panel conversation). Identity is (agent_view_id, sha1(ref)); `key` is the random
-- name of the thread's directory. A job joins its thread at insert (framework/threads.py).
-- `line_id` is the agent a depth > 0 job continues (written once, at insert);
-- `resume_job_id` is the session the job loads (resolved at claim).
CREATE TABLE thread (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  agent_view_id INT UNSIGNED NULL,
  ref VARCHAR(600) NOT NULL,
  ref_hash CHAR(40) NOT NULL,
  `key` CHAR(32) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_thread_ref (agent_view_id, ref_hash),
  UNIQUE KEY uq_thread_key (`key`),
  CONSTRAINT fk_thread_agent_view FOREIGN KEY (agent_view_id) REFERENCES agent_view(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- idx_job_thread serves threads.line_blocked (the few open turns); idx_job_thread_turn
-- serves threads.resolve_resume, a backward scan by finish time that stops at the line's
-- last finished turn, so its cost does not grow with the thread's history.
-- finished_at keeps microseconds: resolve_resume orders a line's turns by finish time, and
-- two turns of one line can finish in the same second in either id order. Every terminal
-- write sets it with NOW(6).
ALTER TABLE job
  MODIFY COLUMN finished_at TIMESTAMP(6) NULL,
  ADD COLUMN thread_id BIGINT UNSIGNED NULL AFTER depth,
  ADD COLUMN resume_job_id BIGINT UNSIGNED NULL AFTER thread_id,
  ADD COLUMN line_id BIGINT UNSIGNED NULL AFTER resume_job_id,
  ADD KEY idx_job_thread (thread_id, depth, status),
  ADD KEY idx_job_thread_turn (thread_id, depth, finished_at),
  ADD KEY idx_job_resume (resume_job_id),
  ADD KEY idx_job_line (line_id, status),
  ADD CONSTRAINT fk_job_thread FOREIGN KEY (thread_id) REFERENCES thread(id) ON DELETE SET NULL,
  ADD CONSTRAINT fk_job_resume FOREIGN KEY (resume_job_id) REFERENCES job(id) ON DELETE SET NULL;

-- The cause of a stretch's last deferral: the line rule (1) or an observer (0). Only a
-- job waiting on its line stays ahead of the later turns of that line (threads.line_blocked).
ALTER TABLE job_defer_stretch
  ADD COLUMN line_wait TINYINT(1) NOT NULL DEFAULT 0 AFTER defer_count;
