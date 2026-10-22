from unittest.mock import patch

from agento.framework.channels import registry
from agento.framework.job_models import AgentType, RequesterTrust
from agento.modules.bitbucket.src.channel import (
    CHANGES_PRIORITY_BUMP,
    SOURCE_CHANGES,
    SOURCE_COMMENTS,
    BitbucketChangesChannel,
    BitbucketCommentsChannel,
    BitbucketPublisher,
)

AGENT = "{agent-uuid}"
REV = "{reviewer-uuid}"
T2 = "2026-01-01T11:00:00+00:00"
T3 = "2026-01-01T12:00:00+00:00"


def _comments_pr():
    return {
        "workspace": "acme", "repo": "api", "id": 42,
        "comments": [
            {"id": "old", "author_uuid": AGENT, "created_on": "2026-01-01T09:00:00+00:00"},
            {"id": "new", "author_uuid": REV, "created_on": T3, "deleted": False, "resolved": False},
        ],
        "commits": [],
    }


def _changes_pr():
    return {
        "workspace": "acme", "repo": "api", "id": 42,
        "changes_requests": [{"user_uuid": REV, "date": T2}],
    }


# --- D-7: registered channel .name == published job.source ----------------------------------------

def test_channel_names_match_published_sources():
    assert BitbucketCommentsChannel().name == SOURCE_COMMENTS == "bitbucket-comments"
    assert BitbucketChangesChannel().name == SOURCE_CHANGES == "bitbucket-changes"


def test_channels_resolve_via_get_channel():
    registry.register_channel(BitbucketCommentsChannel())
    registry.register_channel(BitbucketChangesChannel())
    assert registry.get_channel("bitbucket-comments").name == "bitbucket-comments"
    assert registry.get_channel("bitbucket-changes").name == "bitbucket-changes"


# --- prompt fragments (F10: read + verify OPEN before acting) -------------------------------------

def test_prompt_fragments_instruct_read_and_open_check():
    frag = BitbucketCommentsChannel().get_prompt_fragments("acme/api:42")
    assert "bitbucket_get_pr" in frag.read_context
    assert "OPEN" in frag.read_context
    assert "acme/api:42" in frag.read_context


def test_followup_fragments_include_planning_instructions():
    frag = BitbucketChangesChannel().get_followup_fragments("acme/api:42", "PRZYWITAJ recenzenta")
    assert "PRZYWITAJ recenzenta" in frag.extra
    assert "bitbucket_get_pr" in frag.read_context  # re-read + verify state before acting


# --- publish_pr: comments lane --------------------------------------------------------------------

@patch("agento.modules.bitbucket.src.channel.publish", return_value=True)
def test_publish_pr_comments_lane(mock_publish):
    ok = BitbucketPublisher().publish_pr(
        object(), _comments_pr(), lane="comments", agent_view_id=7, priority=50, account_uuid=AGENT,
    )
    assert ok is True
    args, kwargs = mock_publish.call_args
    assert args[1] is AgentType.TODO
    assert args[2] == SOURCE_COMMENTS
    assert args[3].startswith("bitbucket:comments:acme/api:42:")
    assert kwargs["reference_id"] == "acme/api:42"
    assert kwargs["priority"] == 50  # base priority, no bump
    assert kwargs["skip_if_active"] is True
    assert kwargs["agent_view_id"] == 7
    assert kwargs["thread"] == "bitbucket:acme/api:42"  # one thread per PR, both lanes
    req = kwargs["requester"]
    assert req.key == f"bitbucket:account:{REV}"
    assert req.trust is RequesterTrust.ACCOUNT
    assert req.meta["basis"] == "comments"
    assert req.meta["pr"] == "acme/api:42"


@patch("agento.modules.bitbucket.src.channel.publish", return_value=True)
def test_publish_pr_comments_lane_no_unanswered_skips(mock_publish):
    pr = _comments_pr()
    pr["comments"] = [{"id": "a", "author_uuid": AGENT, "created_on": T3}]  # only agent's own
    ok = BitbucketPublisher().publish_pr(
        object(), pr, lane="comments", agent_view_id=7, priority=50, account_uuid=AGENT,
    )
    assert ok is False
    mock_publish.assert_not_called()


# --- publish_pr: changes lane ---------------------------------------------------------------------

@patch("agento.modules.bitbucket.src.channel.publish", return_value=True)
def test_publish_pr_changes_lane_prioritized(mock_publish):
    ok = BitbucketPublisher().publish_pr(
        object(), _changes_pr(), lane="changes", agent_view_id=7, priority=50, account_uuid=AGENT,
    )
    assert ok is True
    args, kwargs = mock_publish.call_args
    assert args[2] == SOURCE_CHANGES
    assert args[3].startswith("bitbucket:changes:acme/api:42:")
    assert kwargs["priority"] == min(100, 50 + CHANGES_PRIORITY_BUMP)
    assert kwargs["skip_if_active"] is True
    assert kwargs["requester"].meta["basis"] == "changes"
    assert kwargs["thread"] == "bitbucket:acme/api:42"


@patch("agento.modules.bitbucket.src.channel.publish", return_value=True)
def test_publish_pr_changes_lane_priority_capped_at_100(mock_publish):
    BitbucketPublisher().publish_pr(
        object(), _changes_pr(), lane="changes", agent_view_id=7, priority=90, account_uuid=AGENT,
    )
    _, kwargs = mock_publish.call_args
    assert kwargs["priority"] == 100


@patch("agento.modules.bitbucket.src.channel.publish", return_value=True)
def test_publish_pr_changes_lane_no_event_skips(mock_publish):
    pr = {"workspace": "acme", "repo": "api", "id": 42, "changes_requests": []}
    ok = BitbucketPublisher().publish_pr(
        object(), pr, lane="changes", agent_view_id=7, priority=50, account_uuid=AGENT,
    )
    assert ok is False
    mock_publish.assert_not_called()
