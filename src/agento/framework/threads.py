"""Threads: which jobs belong together, and which session a job continues.

A **thread** is the jobs of one conversation of work: the comments on one Jira issue, one
mail conversation, one PR, one panel conversation. Its identity is
`(agent_view_id, sha1(ref))`; `ref` is what the channel names (`jira:AI-1`), else
`default_ref()`. Every job gets its thread when it is inserted (`publisher.insert_job`), and
the claim gives one a thread if an out-of-tree INSERT did not (`place_claim`).

A **line** is the turns of one logical agent in a thread. All `depth = 0` jobs of a thread
are one line. A `depth > 0` job starts a line (its key is its own id) or continues one
(`line_id` = the key, written once at insert). A line never crosses a thread: every query
below also filters on the job's current `thread_id`, so a job moved to another thread at
claim (`detach`) has no earlier turn.

At claim a job waits while an earlier turn of its line is open (`line_blocked`), and its
session target is the latest finished turn of its line that has a session
(`resolve_resume`).
"""
from __future__ import annotations

import hashlib
import secrets

from .job_models import OPEN_STATUSES, TERMINAL_STATUSES, Job

# `thread.ref` is VARCHAR(600): a source (<= 64) + ':' + a reference_id (<= 512) fits.
MAX_REF_LENGTH = 600
# How long a job waits behind an earlier open turn of its line before it is offered again:
# LINE_DEFER_MS, doubled per deferral of the same stretch, up to LINE_DEFER_MAX_MS. The
# cap is the default poll interval, so a turn starts at most that long after its line is
# free, and B waiting turns cost about B / 5 claim passes a second (SCL-1). Framework
# constants (PLC-2: no module's config); the claim clamps the result (defer.py).
LINE_DEFER_MS = 1000
LINE_DEFER_MAX_MS = 5000


def line_defer_ms(defer_count: int) -> int:
    """The delay of a job's `defer_count`-th deferral in a row behind its line."""
    return min(LINE_DEFER_MS << min(max(defer_count - 1, 0), 8), LINE_DEFER_MAX_MS)


def default_ref(source: str, reference_id: str | None, job_id: int) -> str:
    """The thread ref of a job whose caller named none: its source item, else the job itself."""
    return f"{source}:{reference_id}" if reference_id else f"job:{job_id}"


def ensure_thread(cur, *, agent_view_id: int | None, ref: str, lock: bool = False) -> int:
    """The id of the thread `(agent_view_id, ref)`, created when it does not exist.

    Runs on the caller's cursor and does not commit. The SELECT first keeps the common case
    (the thread exists) free of a burnt auto_increment id. Two callers that create one
    thread at once get one row: the unique key makes the second an update that returns the
    first row's id. With no view the unique key does not apply (NULL), so each such job
    gets its own thread. `lock` holds the thread row until the caller commits (see
    `join_thread`): an existing thread is locked by id after a plain read, and a new one by
    the upsert itself, so two first jobs of one thread wait for each other, never deadlock.
    """
    if len(ref) > MAX_REF_LENGTH:
        raise ValueError(f"thread ref longer than {MAX_REF_LENGTH} characters")
    ref_hash = hashlib.sha1(ref.encode()).hexdigest()
    if agent_view_id is not None:
        cur.execute("SELECT id FROM thread WHERE agent_view_id = %s AND ref_hash = %s",
                    (agent_view_id, ref_hash))
        row = cur.fetchone()
        if row is not None:
            if lock:
                cur.execute("SELECT id FROM thread WHERE id = %s FOR UPDATE", (row["id"],))
            return row["id"]
    cur.execute(
        "INSERT INTO thread (agent_view_id, ref, ref_hash, `key`) VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
        (agent_view_id, ref, ref_hash, secrets.token_hex(16)),
    )
    return cur.lastrowid


def join_thread(cur, thread_id: int, *, agent_view_id: int | None, depth: int) -> None:
    """Lock the thread a new job joins by id; at depth 0, check it is a thread of the job's view.

    The lock (held until the caller commits) makes the jobs of one thread commit in id
    order, which `line_blocked` relies on: an earlier turn is never still invisible when a
    later one is claimed. The view check keeps a depth-0 job from joining, and so resuming,
    another view's thread (SEC-7). A depth > 0 job (a sub-agent in another view) may join
    its parent's thread: its line is keyed by its own id or `line_id`, never the view's
    depth-0 line, so it resumes no session of the thread's view.
    """
    if depth == 0 and agent_view_id is None:
        # A thread with no view (its view was deleted, or it never had one) belongs to no
        # one: equal NULLs are not a shared owner.
        raise ValueError("a job with no agent_view cannot join a thread")
    cur.execute("SELECT agent_view_id FROM thread WHERE id = %s FOR UPDATE", (thread_id,))
    row = cur.fetchone()
    if row is None or (depth == 0 and row["agent_view_id"] != agent_view_id):
        raise ValueError(f"thread {thread_id} is not a thread of this job's agent_view")


def line_key(job: Job) -> int | None:
    """The id that names a depth > 0 job's line; None for the thread's depth-0 line."""
    if job.depth == 0:
        return None
    return job.line_id if job.line_id is not None else job.id


def _line_where(job: Job) -> tuple[str, tuple]:
    key = line_key(job)
    if key is None:
        return "thread_id = %s AND depth = 0", (job.thread_id,)
    return "(id = %s OR line_id = %s) AND thread_id = %s", (key, key, job.thread_id)


def place_claim(cur, job: Job, *, detach: bool) -> None:
    """Give a claimed job its thread before the line rules read it. On the claim's cursor.

    A row with no thread (an INSERT that did not go through `insert_job`) gets its default
    thread. `detach` (a `job_claim_before` observer's decision) moves the job to its default
    thread and clears its session, so neither a fresh turn nor a retry continues the old
    thread's context. A job already in its default thread has nothing to move.
    """
    if job.thread_id is not None and not detach:
        return
    ref = default_ref(job.source, job.reference_id, job.id)
    if job.thread_id is not None and job.agent_view_id is None:
        # With no view `ensure_thread` makes a new thread on every call: a job already in
        # its own default thread must not get another one on every claim pass.
        cur.execute("SELECT ref FROM thread WHERE id = %s", (job.thread_id,))
        row = cur.fetchone()
        if row is not None and row["ref"] == ref:
            return
    own = ensure_thread(cur, agent_view_id=job.agent_view_id, ref=ref)
    if own == job.thread_id:
        return
    if detach and job.thread_id is not None:
        cur.execute("UPDATE job SET thread_id = %s, session_id = NULL WHERE id = %s", (own, job.id))
        job.session_id = None
        job.thread_detached = True
    else:
        cur.execute("UPDATE job SET thread_id = %s WHERE id = %s", (own, job.id))
    job.thread_id = own


def lock_thread(cur, thread_id: int) -> str | None:
    """Hold the thread row until the claim commits, so the claims of one thread run one at a
    time. Without it two consumers could each read the other's turn before it changes (a
    turn that comes due between the two reads) and both start. The lock order is job, then
    thread: the claim holds its job row; a publisher of a delegated job locks the parent row
    before the thread (`publisher.insert_job`), and the toolbox INSERT's foreign key checks
    lock the parent row before the thread row (InnoDB checks them in constraint-name order:
    `fk_job_parent` < `fk_job_thread`; tests/integration/test_subagent_wake.py).

    Returns the thread's random `key`, the name of its dir (`artifacts_dir.thread_dir`).
    """
    cur.execute("SELECT `key` FROM thread WHERE id = %s FOR UPDATE", (thread_id,))
    row = cur.fetchone()
    return None if row is None else row["key"]


def line_blocked(cur, job: Job) -> bool:
    """Whether another open turn of the job's line goes first, so this job must wait.

    A turn that has started goes before one that has not; among equals the lower id goes
    first. RUNNING and PAUSED have started, so they always hold the line: a job never runs
    beside another open turn, whatever its id. One exception: a PAUSED job with no session
    can never resume (`job_store.resume_job`), so it holds nothing. A TODO that has started
    (a retry, a pool wait) holds every turn that has not, and a started one with a higher
    id. A TODO that has not started holds a later turn only while it is queued: due, or
    waiting on this line (an open defer stretch whose last deferral was the line rule's).
    A deferral by an observer does not count: that observer orders by its own rule, which
    need not be job-id order, and the two rules would wait for each other. A follow-up
    scheduled for later that never ran does not hold the line, so a later turn may pass
    it; `resolve_resume` then gives each its true predecessor.
    """
    where, params = _line_where(job)
    if job.started_at is not None:
        todo_first = "started_at IS NOT NULL AND id < %s"
    else:
        todo_first = (
            "started_at IS NOT NULL OR (id < %s AND (scheduled_after <= NOW() OR EXISTS ("
            "SELECT 1 FROM job_defer_stretch s WHERE s.job_id = job.id AND s.closed_at IS NULL "
            "AND s.line_wait = 1)))")
    # The open turns are few: read them by status, never the thread's history (CODE-8).
    hint = "FORCE INDEX (idx_job_thread) " if line_key(job) is None else ""
    cur.execute(
        f"SELECT 1 FROM job {hint}WHERE {where} AND id <> %s AND status IN %s "
        "AND (status = 'RUNNING' OR (status = 'PAUSED' AND session_id IS NOT NULL) "
        f"OR (status = 'TODO' AND ({todo_first}))) LIMIT 1",
        (*params, job.id, OPEN_STATUSES, job.id))
    return cur.fetchone() is not None


def resolve_resume(cur, job: Job) -> int | None:
    """The turn of the job's line that finished last and has a session, or None (fresh).

    Following the line to its last turn keeps one session linear, so a wake and a new
    comment never fork it. Last FINISHED, not highest id: a later turn may pass a
    follow-up that was scheduled for later (`line_blocked`), and the follow-up must then
    continue that turn's session. One open turn per line makes the finish order the run
    order. `finished_at` keeps microseconds (every terminal write uses NOW(6)), because two
    turns of a line can finish within one second in either id order.
    """
    where, params = _line_where(job)
    # The thread's depth-0 line can be long (a periodic task on one reference). The
    # thread's own index stops at its last turn; left to itself, the optimizer may walk a
    # wider index over every job of every thread.
    hint = "FORCE INDEX (idx_job_thread_turn) " if line_key(job) is None else ""
    cur.execute(
        f"SELECT id FROM job {hint}WHERE {where} AND id <> %s AND status IN %s "
        "AND session_id IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1",
        (*params, job.id, TERMINAL_STATUSES),
    )
    row = cur.fetchone()
    return None if row is None else row["id"]
