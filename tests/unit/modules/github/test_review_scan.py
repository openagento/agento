from agento.modules.github.src.review_scan import (
    build_changes_key,
    build_comments_key,
    comments_key_parts,
    detect_changes_requested,
    flag_unanswered,
    lane_data_is_complete,
    latest_commit_on,
)

AGENT = "agent-bot"


def _c(login, created_at, **kw):
    return {"author_login": login, "created_at": created_at, "surface": "issue", **kw}


def test_latest_commit_is_max_not_first():
    assert latest_commit_on([
        {"date": "2026-01-01T10:00:00Z"},
        {"date": "2026-01-03T10:00:00Z"},
        {"date": "2026-01-02T10:00:00Z"},
    ]) == "2026-01-03T10:00:00Z"
    assert latest_commit_on([]) is None
    assert latest_commit_on(None) is None


def test_unanswered_requires_newer_than_agent_comment_and_last_commit():
    pr = {
        "comments": [
            _c("alice", "2026-01-01T00:00:00Z"),
            _c(AGENT, "2026-01-02T00:00:00Z"),
            _c("alice", "2026-01-03T00:00:00Z"),
        ],
        "commits": [{"date": "2026-01-02T12:00:00Z"}],
    }
    out = flag_unanswered(pr, AGENT)
    assert [c["created_at"] for c in out] == ["2026-01-03T00:00:00Z"]


def test_feedback_in_the_agents_own_second_is_still_flagged():
    # GitHub timestamps are second-precision and nothing orders a reviewer comment against the agent's
    # reply inside one second, so equality must count as actionable — the strict comparison dropped this
    # comment forever.
    pr = {
        "comments": [
            _c(AGENT, "2026-01-02T12:00:00Z", id=10),
            _c("alice", "2026-01-02T12:00:00Z", id=11),
        ],
        "commits": [],
    }
    assert [c["id"] for c in flag_unanswered(pr, AGENT)] == [11]


def test_feedback_in_the_head_commits_own_second_is_still_flagged():
    # Same tie against the force-push watermark: a comment sharing the head commit's second is not
    # evidence that the push answered it.
    pr = {
        "comments": [_c("alice", "2026-01-04T00:00:00Z", id=11)],
        "commits": [{"date": "2026-01-04T00:00:00Z"}],
    }
    assert [c["id"] for c in flag_unanswered(pr, AGENT)] == [11]


def test_force_push_after_feedback_clears_it():
    pr = {
        "comments": [_c("alice", "2026-01-03T00:00:00Z")],
        "commits": [{"date": "2026-01-04T00:00:00Z"}],
    }
    assert flag_unanswered(pr, AGENT) == []


def test_resolved_thread_counts_as_addressed():
    pr = {"comments": [_c("alice", "2026-01-03T00:00:00Z", surface="review", resolved=True)], "commits": []}
    assert flag_unanswered(pr, AGENT) == []


def test_unanswered_spans_all_three_surfaces_and_is_chronological():
    pr = {
        "comments": [
            _c("alice", "2026-01-05T00:00:00Z", surface="review_body"),
            _c("bob", "2026-01-03T00:00:00Z", surface="review"),
            _c("carol", "2026-01-04T00:00:00Z", surface="issue"),
        ],
        "commits": [],
    }
    out = flag_unanswered(pr, AGENT)
    assert [c["author_login"] for c in out] == ["bob", "carol", "alice"]


def test_agent_comment_on_any_surface_moves_the_watermark():
    pr = {
        "comments": [
            _c("alice", "2026-01-01T00:00:00Z", surface="issue"),
            _c(AGENT, "2026-01-02T00:00:00Z", surface="review"),
        ],
        "commits": [],
    }
    assert flag_unanswered(pr, AGENT) == []


def test_login_match_is_case_insensitive():
    pr = {"comments": [_c("Agent-Bot", "2026-01-09T00:00:00Z")], "commits": []}
    assert flag_unanswered(pr, AGENT) == []


def test_truncated_decision_data_blocks_the_lane():
    """G-18: a capped scan means "unknown", not "nothing there"."""
    assert lane_data_is_complete({}, "comments") is True
    assert lane_data_is_complete({"truncated": []}, "changes") is True
    assert lane_data_is_complete({"truncated": ["reviews"]}, "changes") is False
    assert lane_data_is_complete({"truncated": ["issue_comments"]}, "comments") is False
    # A scan the OTHER lane depends on does not block this one.
    assert lane_data_is_complete({"truncated": ["issue_comments"]}, "changes") is True
    # An unreadable head commit is an unknown force-push watermark: the comments lane must not answer.
    assert lane_data_is_complete({"truncated": ["head_commit"]}, "comments") is False
    assert lane_data_is_complete({"truncated": ["head_commit"]}, "changes") is True


def test_comments_without_a_login_are_ignored():
    """G-17: a deleted/ghost GitHub account yields user: null; it must never become a requester."""
    pr = {"comments": [{"author_login": None, "created_at": "2026-01-03T00:00:00Z", "surface": "issue"}],
          "commits": []}
    assert flag_unanswered(pr, AGENT) == []


def test_unparseable_timestamps_are_skipped_not_crashing():
    pr = {"comments": [_c("alice", "not-a-date"), _c("alice", "2026-01-03T00:00:00Z")], "commits": []}
    assert [c["created_at"] for c in flag_unanswered(pr, AGENT)] == ["2026-01-03T00:00:00Z"]


def _r(login, date, state="CHANGES_REQUESTED"):
    return {"user_login": login, "date": date, "state": state}


def test_changes_requested_picks_newest_non_agent_reviewer():
    pr = {"reviews": [
        _r("alice", "2026-01-01T00:00:00Z"),
        _r("bob", "2026-01-05T00:00:00Z"),
        _r(AGENT, "2026-01-09T00:00:00Z"),
    ]}
    assert detect_changes_requested(pr, AGENT)["user_login"] == "bob"
    assert detect_changes_requested({"reviews": []}, AGENT) is None
    assert detect_changes_requested({}, AGENT) is None


def test_a_later_approval_by_the_same_reviewer_clears_the_request():
    """G-16: /pulls/{n}/reviews is a full history, not current state."""
    pr = {"reviews": [
        _r("alice", "2026-01-01T00:00:00Z"),
        _r("alice", "2026-01-02T00:00:00Z", state="APPROVED"),
    ]}
    assert detect_changes_requested(pr, AGENT) is None


def test_a_dismissed_request_is_cleared_and_a_re_request_re_arms():
    pr = {"reviews": [_r("alice", "2026-01-01T00:00:00Z", state="DISMISSED")]}
    assert detect_changes_requested(pr, AGENT) is None
    pr = {"reviews": [
        _r("alice", "2026-01-01T00:00:00Z"),
        _r("alice", "2026-01-02T00:00:00Z", state="APPROVED"),
        _r("alice", "2026-01-03T00:00:00Z"),
    ]}
    assert detect_changes_requested(pr, AGENT)["date"] == "2026-01-03T00:00:00Z"


def test_comment_and_pending_reviews_do_not_change_state():
    pr = {"reviews": [
        _r("alice", "2026-01-01T00:00:00Z"),
        _r("alice", "2026-01-02T00:00:00Z", state="COMMENTED"),
        _r("alice", "2026-01-03T00:00:00Z", state="PENDING"),
    ]}
    assert detect_changes_requested(pr, AGENT)["date"] == "2026-01-01T00:00:00Z"


def test_one_reviewer_approving_does_not_clear_another_s_request():
    pr = {"reviews": [
        _r("alice", "2026-01-01T00:00:00Z"),
        _r("bob", "2026-01-02T00:00:00Z", state="APPROVED"),
    ]}
    assert detect_changes_requested(pr, AGENT)["user_login"] == "alice"


def test_idempotency_keys_carry_the_timestamp_and_the_identities():
    key = build_comments_key("acme/api:7", "2026-01-03T00:00:00Z", [("review", 991)])
    assert key.startswith("github:comments:acme/api:7:2026-01-03T00:00:00Z:")
    assert key == build_comments_key("acme/api:7", "2026-01-03T00:00:00Z", [("review", 991)])  # stable
    assert (build_changes_key("acme/api:7", "2026-01-05T00:00:00Z", 55)
            == "github:changes:acme/api:7:2026-01-05T00:00:00Z:55")


def test_same_second_different_id_yields_different_keys():
    """GitHub timestamps are second-precision: a timestamp-only key would drop the second comment."""
    ts = "2026-01-03T00:00:00Z"
    assert (build_comments_key("acme/api:7", ts, [("review", 991)])
            != build_comments_key("acme/api:7", ts, [("review", 992)]))
    assert build_changes_key("acme/api:7", ts, 55) != build_changes_key("acme/api:7", ts, 56)


def test_same_id_in_different_surfaces_yields_different_keys():
    """Issue-comment ids and review-comment ids are separate namespaces and may be equal."""
    ts = "2026-01-03T00:00:00Z"
    assert (build_comments_key("acme/api:7", ts, [("issue", 7)])
            != build_comments_key("acme/api:7", ts, [("review", 7)]))


def test_a_same_second_sibling_changes_the_key_whatever_the_order():
    """The whole same-second set is the key input, so a new sibling can never be silently absorbed —
    even when its own id sorts BEFORE the already-published one (different surfaces, different id
    namespaces). A set-insensitive key is exactly how genuinely new feedback goes unqueued forever."""
    ts = "2026-01-03T00:00:00Z"
    one = build_comments_key("acme/api:7", ts, [("review", 991)])
    two = build_comments_key("acme/api:7", ts, [("review", 991), ("issue", 5)])
    assert one != two
    # ...and the result does not depend on the order the surfaces were concatenated in.
    assert two == build_comments_key("acme/api:7", ts, [("issue", 5), ("review", 991)])


def test_comments_key_parts_returns_every_identity_at_the_newest_second():
    pr = {"comments": [
        _c("alice", "2026-01-03T00:00:00Z", id=5, surface="issue"),
        _c("bob", "2026-01-03T00:00:00Z", id=991, surface="review"),
        _c("alice", "2026-01-02T00:00:00Z", id=4, surface="issue"),
    ], "commits": []}
    created_at, identities = comments_key_parts(flag_unanswered(pr, AGENT))
    assert created_at == "2026-01-03T00:00:00Z"
    assert sorted(identities) == [("issue", 5), ("review", 991)]  # the older comment is not included


def test_a_same_second_approval_retires_the_change_request():
    """Second-precision ties are ordinary. Review ids are monotonic, so the later-submitted APPROVED
    (higher id) is the reviewer's current position even when both carry the same timestamp."""
    ts = "2026-01-01T00:00:00Z"
    pr = {"reviews": [
        {"id": 10, "user_login": "alice", "date": ts, "state": "CHANGES_REQUESTED"},
        {"id": 11, "user_login": "alice", "date": ts, "state": "APPROVED"},
    ]}
    assert detect_changes_requested(pr, AGENT) is None
    # ...and the ordering is on the id's VALUE, not its text: "9" must not outrank "10".
    pr_lex = {"reviews": [
        {"id": 9, "user_login": "alice", "date": ts, "state": "APPROVED"},
        {"id": 10, "user_login": "alice", "date": ts, "state": "CHANGES_REQUESTED"},
    ]}
    assert detect_changes_requested(pr_lex, AGENT)["id"] == 10
    # Input order must not change either verdict.
    assert detect_changes_requested({"reviews": list(reversed(pr["reviews"]))}, AGENT) is None
