from unittest.mock import patch

from agento.modules.github.src.channel import (
    CHANGES_PRIORITY_BUMP,
    LANE_CHANGES,
    LANE_COMMENTS,
    SOURCE_CHANGES,
    SOURCE_COMMENTS,
    GitHubChangesChannel,
    GitHubCommentsChannel,
    GitHubPublisher,
)

AGENT = "agent-bot"
PR = {"owner": "acme", "repo": "api", "id": 7}


def test_reference_id_shape():
    assert GitHubPublisher.reference_id(PR) == "acme/api:7"


def test_channel_names_match_published_sources():
    assert GitHubCommentsChannel().name == SOURCE_COMMENTS == "github-comments"
    assert GitHubChangesChannel().name == SOURCE_CHANGES == "github-changes"


def test_prompt_fragments_mention_the_open_state_gate():
    frags = GitHubCommentsChannel().get_prompt_fragments("acme/api:7")
    assert "acme/api:7" in frags.read_context
    assert "github_get_pr" in frags.read_context
    assert "OTWART" in frags.read_context  # must confirm the PR is still open first


def test_publish_comments_lane_publishes_once_with_base_priority():
    pr = {**PR, "comments": [{"id": 991, "author_login": "alice", "created_at": "2026-01-03T00:00:00Z",
                              "surface": "review"}], "commits": []}
    with patch("agento.modules.github.src.channel.publish", return_value=True) as pub:
        assert GitHubPublisher().publish_pr(
            object(), pr, lane=LANE_COMMENTS, agent_view_id=3, priority=50, login=AGENT
        ) is True
    args, kwargs = pub.call_args
    assert args[2] == SOURCE_COMMENTS
    # timestamp + a digest of every identity at that second, not the timestamp alone: two comments in
    # the same second must not collide, and a same-second sibling must change the key.
    assert args[3].startswith("github:comments:acme/api:7:2026-01-03T00:00:00Z:")
    assert args[3] != "github:comments:acme/api:7:2026-01-03T00:00:00Z:"
    assert kwargs["reference_id"] == "acme/api:7"
    assert kwargs["priority"] == 50
    assert kwargs["skip_if_active"] is True
    assert kwargs["requester"].key == "github:login:alice"
    assert kwargs["requester"].trust.name == "ACCOUNT"


def test_publish_changes_lane_bumps_priority_and_caps_at_100():
    pr = {**PR, "reviews": [{"id": 55, "user_login": "bob", "date": "2026-01-05T00:00:00Z",
                             "state": "CHANGES_REQUESTED"}]}
    with patch("agento.modules.github.src.channel.publish", return_value=True) as pub:
        GitHubPublisher().publish_pr(object(), pr, lane=LANE_CHANGES, agent_view_id=3, priority=50, login=AGENT)
    assert pub.call_args.args[3] == "github:changes:acme/api:7:2026-01-05T00:00:00Z:55"
    assert pub.call_args.kwargs["priority"] == 50 + CHANGES_PRIORITY_BUMP
    with patch("agento.modules.github.src.channel.publish", return_value=True) as pub:
        GitHubPublisher().publish_pr(object(), pr, lane=LANE_CHANGES, agent_view_id=3, priority=95, login=AGENT)
    assert pub.call_args.kwargs["priority"] == 100


def test_no_work_publishes_nothing():
    with patch("agento.modules.github.src.channel.publish") as pub:
        assert GitHubPublisher().publish_pr(
            object(), {**PR, "comments": [], "commits": []},
            lane=LANE_COMMENTS, agent_view_id=3, priority=50, login=AGENT,
        ) is False
        assert GitHubPublisher().publish_pr(
            object(), {**PR, "reviews": []},
            lane=LANE_CHANGES, agent_view_id=3, priority=50, login=AGENT,
        ) is False
    pub.assert_not_called()


def test_incomplete_scan_blocks_publishing():
    """G-18: even with obvious work present, a truncated decision scan must not publish."""
    pr = {**PR, "truncated": ["reviews"],
          "reviews": [{"user_login": "bob", "date": "2026-01-05T00:00:00Z", "state": "CHANGES_REQUESTED"}]}
    with patch("agento.modules.github.src.channel.publish") as pub:
        assert GitHubPublisher().publish_pr(
            object(), pr, lane=LANE_CHANGES, agent_view_id=3, priority=50, login=AGENT
        ) is False
    pub.assert_not_called()


def test_an_unreadable_head_commit_blocks_the_comments_lane():
    """G-18: no watermark ⇒ the comments lane cannot tell a force-push from silence."""
    pr = {**PR, "truncated": ["head_commit"], "commits": [],
          "comments": [{"author_login": "alice", "created_at": "2026-01-09T00:00:00Z",
                        "surface": "issue", "resolved": False}]}
    with patch("agento.modules.github.src.channel.publish") as pub:
        assert GitHubPublisher().publish_pr(
            object(), pr, lane=LANE_COMMENTS, agent_view_id=3, priority=50, login=AGENT
        ) is False
    pub.assert_not_called()
    # The same PR on the changes lane is unaffected — that lane never reads a commit.
    with patch("agento.modules.github.src.channel.publish") as pub:
        GitHubPublisher().publish_pr(
            object(), {**pr, "reviews": [{"user_login": "bob", "date": "2026-01-05T00:00:00Z",
                                          "state": "CHANGES_REQUESTED"}]},
            lane=LANE_CHANGES, agent_view_id=3, priority=50, login=AGENT,
        )
    pub.assert_called_once()


def test_a_requester_is_never_minted_without_an_identity():
    """G-17: no login ⇒ no job (never a `github:login:None` ACCOUNT-trust requester)."""
    pr = {**PR, "comments": [{"author_login": None, "created_at": "2026-01-03T00:00:00Z"}], "commits": []}
    with patch("agento.modules.github.src.channel.publish") as pub:
        assert GitHubPublisher().publish_pr(
            object(), pr, lane=LANE_COMMENTS, agent_view_id=3, priority=50, login=AGENT
        ) is False
    pub.assert_not_called()


def test_unknown_lane_raises():
    import pytest
    with pytest.raises(ValueError):
        GitHubPublisher().publish_pr(object(), PR, lane="nope", agent_view_id=1, priority=1, login=AGENT)
