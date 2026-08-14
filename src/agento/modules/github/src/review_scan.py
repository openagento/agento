"""Pure PR-review scan logic — no I/O, unit-tested in isolation.

The toolbox returns a normalized per-PR record (see toolbox/api-handlers.js); these functions decide,
from that record alone, whether a PR has work for a given lane. Everything is computed client-side by
``max(date)`` / timestamp-filter so the result never depends on the order the GitHub API happened to
return collections in, and so the three GitHub comment surfaces (issue comments, inline review
comments, review bodies) can be merged into one ordering.
"""
from __future__ import annotations

import hashlib
from datetime import datetime


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (``2026-01-02T03:04:05Z``) to an aware datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same_login(a: str | None, b: str | None) -> bool:
    """GitHub logins are case-insensitive (``Agent-Bot`` == ``agent-bot``)."""
    if not a or not b:
        return False
    return a.lower() == b.lower()


def _id_key(value: object) -> tuple[int, int, str]:
    """Total, order-independent ordering for a GitHub id — numerically when it is one.

    ``str(id)`` alone would sort ``"10"`` before ``"9"``, which defeats the point of tie-breaking on a
    monotonic id. Non-numeric ids (never seen from the API, but the record is data) sort after numeric
    ones by their text, so the ordering stays total whatever arrives.
    """
    try:
        return (0, int(value), "")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return (1, 0, str(value))


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


# Which scans each lane's verdict depends on. A truncated scan here means "unknown", not "absent".
# ``head_commit`` is a comments-lane dependency because it IS that lane's force-push watermark; the
# changes lane keys off the review's own ``submitted_at`` and never reads a commit.
LANE_REQUIRED_SCANS = {
    "comments": ("issue_comments", "review_comments", "reviews", "head_commit"),
    "changes": ("reviews",),
}


def lane_data_is_complete(pr: dict, lane: str) -> bool:
    """False when a scan this lane's decision depends on hit its page cap or failed (G-18)."""
    truncated = set(pr.get("truncated") or ())
    return not truncated.intersection(LANE_REQUIRED_SCANS.get(lane, ()))


def latest_commit_on(commits: list[dict] | None) -> str | None:
    """Newest commit timestamp = ``max(commit.date)`` over a bounded commits window.

    Order-independent, and an empty/absent list ⇒ ``None`` (e.g. the head branch was deleted), so the
    caller's watermark then falls back to the agent's last comment.
    """
    if not commits:
        return None
    best_raw: str | None = None
    best_dt: datetime | None = None
    for c in commits:
        raw = c.get("date") if isinstance(c, dict) else None
        dt = _parse_iso(raw)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt, best_raw = dt, raw
    return best_raw


def flag_unanswered(pr: dict, login: str) -> list[dict]:
    """Return the non-agent comments at or after the watermark (chronological order).

    "Unanswered" = a non-resolved comment authored by someone other than the agent whose ``created_at``
    is at or after BOTH the agent's last comment AND the PR's last commit (a timestamp watermark —
    survives force-push). The comparison is ``>=``, not ``>``: GitHub timestamps are SECOND-precision,
    so a reviewer follow-up posted in the same second as the agent's reply (or as the head commit) is
    indistinguishable from one posted just before it, and no total order exists across the three
    comment surfaces and the commit list to break that tie. Equality is therefore treated as
    actionable, which is the conservative direction: at worst it queues ONE unnecessary job — when the
    real order inside that second was "feedback, then answer" and no job had been published for it yet
    — and every later scan dedupes on the unchanged idempotency key. ``>`` instead discards a real
    "answer, then follow-up" permanently. One bounded false positive beats silent, unrecoverable loss.

    The comment list spans all three GitHub surfaces (issue comments, inline review comments, review
    bodies), so an agent reply on ANY surface moves the watermark. Only inline review threads can be
    resolved (``resolved`` is always False for the other surfaces).
    """
    comments = pr.get("comments") or []

    agent_times = [
        dt
        for c in comments
        if _same_login(c.get("author_login"), login)
        if (dt := _parse_iso(c.get("created_at"))) is not None
    ]
    agent_last = max(agent_times) if agent_times else None
    last_commit = _parse_iso(latest_commit_on(pr.get("commits")))
    watermark = _latest(agent_last, last_commit)

    unanswered = []
    for c in comments:
        if c.get("resolved"):
            continue
        # No login ⇒ a deleted/ghost account. The toolbox already drops these (G-17); belt-and-braces
        # here because publish_pr turns the author into a RequesterTrust.ACCOUNT requester key.
        if not c.get("author_login"):
            continue
        if _same_login(c.get("author_login"), login):
            continue
        created = _parse_iso(c.get("created_at"))
        if created is None:
            continue
        if watermark is None or created >= watermark:
            unanswered.append(c)

    # GitHub timestamps are second-precision, so ties are ordinary, not exotic. Sorting by timestamp
    # alone would leave the order of a tie decided by the surface-concatenation order the toolbox
    # happened to use — i.e. by input order, which this module refuses to depend on. The (surface, id)
    # tail makes the order total and deterministic. It does NOT by itself make the idempotency key
    # sensitive to a same-second sibling — that is what ``comments_key_parts`` below is for.
    unanswered.sort(key=lambda c: (_parse_iso(c.get("created_at")) or datetime.min,
                                   str(c.get("surface")), _id_key(c.get("id"))))
    return unanswered


def comments_key_parts(unanswered: list[dict]) -> tuple[str, list[tuple[str, object]]]:
    """The idempotency inputs for the comments lane: the newest timestamp + EVERY identity at it.

    Keying off the single newest comment is not enough. Two comments can share a second, and their ids
    come from different id namespaces per surface, so a genuinely new comment can sort *before* the one
    already published — leaving the key unchanged and the new feedback never queued. Returning the whole
    same-second set makes the key change whenever that set changes (an addition or a deletion).
    """
    newest = unanswered[-1]  # flag_unanswered returns a total, deterministic order; last = newest
    newest_dt = _parse_iso(newest.get("created_at"))
    identities = [
        (str(c.get("surface")), c.get("id"))
        for c in unanswered
        if _parse_iso(c.get("created_at")) == newest_dt
    ]
    return newest.get("created_at"), identities


# GitHub review states. COMMENTED/PENDING do NOT change a reviewer's decision (a comment-only review
# leaves an earlier approval or change-request standing), so they are ignored when folding the history
# down to each reviewer's CURRENT position. DISMISSED explicitly retracts one. (OV-1 confirms spelling.)
CHANGES_REQUESTED = "CHANGES_REQUESTED"
_NON_DECIDING_STATES = frozenset({"COMMENTED", "PENDING"})


def detect_changes_requested(pr: dict, login: str) -> dict | None:
    """Return the newest review whose reviewer's CURRENT position is "changes requested", or None.

    ``pr["reviews"]`` is the full non-agent review history from ``GET /pulls/{n}/reviews`` — GitHub
    never removes a superseded review, so "any CHANGES_REQUESTED ever submitted" would keep flagging a
    PR whose reviewer has since approved (G-16). We therefore fold the history per reviewer: the
    latest DECIDING review (ignoring COMMENTED/PENDING) is that reviewer's current position, and only
    reviewers currently at CHANGES_REQUESTED count. Among those, ``max(date)`` wins, so multiple
    reviewers resolve deterministically to the latest outstanding request.
    """
    # (timestamp, id): second-precision timestamps tie routinely, and a tie broken by input order would
    # let a stale CHANGES_REQUESTED outlive an APPROVED submitted in the same second. Review ids are
    # monotonic, so the id is the correct — and order-independent — tie-break.
    def rank(e: dict) -> tuple[datetime, tuple[int, int, str]]:
        return (_parse_iso(e.get("date")) or datetime.min, _id_key(e.get("id")))

    latest_by_reviewer: dict[str, dict] = {}
    for e in pr.get("reviews") or []:
        who = e.get("user_login")
        if not who or _same_login(who, login):
            continue
        if (e.get("state") or "").upper() in _NON_DECIDING_STATES:
            continue
        if _parse_iso(e.get("date")) is None:
            continue
        key = who.lower()
        previous = latest_by_reviewer.get(key)
        if previous is None or rank(e) > rank(previous):
            latest_by_reviewer[key] = e

    outstanding = [
        e for e in latest_by_reviewer.values() if (e.get("state") or "").upper() == CHANGES_REQUESTED
    ]
    if not outstanding:
        return None
    return max(outstanding, key=rank)


def build_comments_key(
    reference_id: str, newest_unanswered_created_at: str, identities: list[tuple[str, object]]
) -> str:
    """Idempotency key for the comments lane — a no-op rescan dedupes, genuinely new feedback re-queues.

    The timestamp alone is NOT enough: GitHub timestamps are second-precision, so two comments posted in
    the same second would collide. Neither is "the timestamp plus the newest comment's identity": the
    three surfaces have independent id namespaces, so a genuinely new comment can sort *before* the
    published one under any total order, leaving the key unchanged and that feedback never queued. The
    key therefore folds in EVERY identity at the newest second (``surface:id``, sorted so the result is
    input-order-independent), digested to keep the key a bounded length. Any addition or deletion in
    that set changes the digest; an unchanged set reproduces it exactly.
    """
    tail = ",".join(sorted(f"{surface}:{cid}" for surface, cid in identities))
    digest = hashlib.sha1(tail.encode("utf-8")).hexdigest()[:12]
    return f"github:comments:{reference_id}:{newest_unanswered_created_at}:{digest}"


def build_changes_key(reference_id: str, changes_request_date: str, review_id: object) -> str:
    """Idempotency key for the changes lane — the newest CHANGES_REQUESTED submission time plus its
    review id, for the same second-precision reason as ``build_comments_key``."""
    return f"github:changes:{reference_id}:{changes_request_date}:{review_id}"
