from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from agento.modules.outlook.src.channel import OutlookAdmission, _matches_allowed
from agento.modules.outlook.src.commands.publish import publish_all_views

P = "agento.modules.outlook.src.commands.publish"


def _views(*specs):
    # specs: (id, code)
    return [SimpleNamespace(id=i, code=c) for (i, c) in specs]


def _cfg(enabled=True, poll_top=10, allowed="sklep@x.com", mailbox=None, **extra):
    # Raw per-path strings, as ScopedConfigService.get() returns them (the publisher now reads
    # non-secret outlook config per path, never via get_module). The mailbox UPN groups views
    # BEFORE polling: a UPN owned by one view = direct mode, shared by >=2 = routed mode.
    # `**extra` maps to extra outlook/<key> paths (e.g. summon_token=...) for activation-divergence.
    d = {
        "outlook/enabled": "1" if enabled else "0",
        "outlook/poll_top": str(poll_top),
        "outlook/allowed_senders": allowed,
    }
    if mailbox is not None:
        d["outlook/outlook_mailbox_user_id"] = mailbox
    for k, v in extra.items():
        d[f"outlook/{k}"] = v
    return d


def _admit_union(message_id, *, sender_email=None, allowed_senders=None, dmarc=None, **kw):
    """Stand-in for admit_mail that actually EXERCISES the union pre-filter: admit only if the sender
    matches the passed allow-list (the union in routed mode) AND DMARC passes."""
    s = (sender_email or "").strip().lower()
    if not _matches_allowed(s, allowed_senders):
        return None
    if (dmarc or "").lower() != "pass":
        return None
    return OutlookAdmission(sender=s)


def _real_sender_allowed(sender, allowed_senders):
    """Real matcher for pub.sender_allowed (the post-route per-view refinement)."""
    return _matches_allowed((sender or "").strip().lower(), allowed_senders)


class _FakeScoped:
    """Stand-in for ScopedConfigService: serves configs[scope_id] as per-path .get() values."""
    configs: ClassVar[dict] = {}

    def __init__(self, conn, scope, scope_id):
        self._scope_id = scope_id

    def get(self, path):
        return _FakeScoped.configs.get(self._scope_id, {}).get(path)


def _patch_env(configs, list_delta_side_effect):
    _FakeScoped.configs = configs
    client = MagicMock()
    client.list_delta.side_effect = list_delta_side_effect
    pub = MagicMock()
    pub.publish_mail.return_value = True
    return client, pub


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_multi_view_fans_each_mailbox_to_its_own_view(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    responses = {
        1: {"mailbox": "dev@x.com", "deltaLink": "L1", "messages": [{"id": "d1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]},
        2: {"mailbox": "ops@x.com", "deltaLink": "L2", "messages": [{"id": "o1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]},
    }
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com"), 2: _cfg(mailbox="ops@x.com")},
                             lambda top, *, agent_view_id, cursors: responses[agent_view_id])
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 2
    by_view = {c.kwargs["agent_view_id"]: c.args[1] for c in pub.publish_mail.call_args_list}
    assert by_view == {1: "d1", 2: "o1"}
    prios = {c.kwargs["agent_view_id"]: c.kwargs["priority"] for c in pub.publish_mail.call_args_list}
    assert prios == {1: 51, 2: 52}
    client.close.assert_called_once()


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_subject_is_forwarded_to_publish_mail(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    mock_gaav.return_value = _views((1, "dev"))
    resp = {"mailbox": "dev@x.com", "deltaLink": "L", "messages": [
        {"id": "d1", "subject": "Re: Faktura", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com")}, lambda top, *, agent_view_id, cursors: resp)
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert pub.publish_mail.call_args.kwargs["subject"] == "Re: Faktura"


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_auto_reply_flag_is_forwarded_to_publish_mail(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    # Direct mode: the toolbox-derived auto_reply boolean must reach admit_mail via publish_mail.
    mock_gaav.return_value = _views((1, "dev"))
    resp = {"mailbox": "dev@x.com", "deltaLink": "L", "messages": [
        {"id": "d1", "from": {"address": "sklep@x.com"}, "dmarc": "fail", "auto_reply": True}]}
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com")}, lambda top, *, agent_view_id, cursors: resp)
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert pub.publish_mail.call_args.kwargs["auto_reply"] is True


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_disabled_view_is_skipped_and_not_polled(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    client, pub = _patch_env({1: _cfg(enabled=False), 2: _cfg(enabled=True, mailbox="ops@x.com")},
                             lambda top, *, agent_view_id, cursors: {"mailbox": "ops@x.com", "deltaLink": "L", "messages": []})
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    polled = [c.kwargs["agent_view_id"] for c in client.list_delta.call_args_list]
    assert polled == [2]


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_per_view_poll_top_and_allowed_senders_honored(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    mock_gaav.return_value = _views((1, "dev"))
    client, pub = _patch_env({1: _cfg(poll_top=25, allowed="a@x.com, b@y.com", mailbox="dev@x.com")},
                             lambda top, *, agent_view_id, cursors: {"mailbox": "dev@x.com", "deltaLink": "L",
                                                                     "messages": [{"id": "d1", "from": {"address": "a@x.com"}, "dmarc": "pass"}]})
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert client.list_delta.call_args.kwargs["top"] == 25
    assert pub.publish_mail.call_args.kwargs["allowed_senders"] == ["a@x.com", "b@y.com"]


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type")
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_direct_mode_does_not_route(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # A UPN owned by exactly one view is DIRECT mode: no routing, no binding lookup, byte-for-byte
    # the old publish_mail path.
    mock_gaav.return_value = _views((1, "dev"))
    resp = {"mailbox": "dev@x.com", "deltaLink": "L",
            "messages": [{"id": "d1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com")}, lambda top, *, agent_view_id, cursors: resp)
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 1
    mock_resolve.assert_not_called()  # router not consulted in direct mode
    mock_bindings.assert_not_called()  # no zero-bindings check in direct mode
    pub.publish_mail.assert_called_once()
    assert pub.publish_mail.call_args.kwargs["agent_view_id"] == 1


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_agent_view_filter_keeps_shared_group_routed(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # --agent-view selects the group CONTAINING that view but does NOT shrink it: a shared mailbox
    # stays routed (the other member remains a valid target).
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"), (3, "solo"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"},
        {"id": "s2", "from": {"address": "boss@x.com"}, "dmarc": "pass"},
    ]}
    responses = {1: shared, 3: {"mailbox": "solo@x.com", "deltaLink": "Ls", "messages": []}}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com"), 2: _cfg(mailbox="shared@x.com"), 3: _cfg(mailbox="solo@x.com")},
        lambda top, *, agent_view_id, cursors: responses[agent_view_id],
    )
    pub.admit_mail.side_effect = _admit_by_sender
    pub.publish_admitted_mail.return_value = True
    route = {"sklep@x.com": 1, "boss@x.com": 2}
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(route[ctx.identity_value])
    MockClient.return_value = client
    MockPub.return_value = pub

    # filter on "dev" — only the shared group is processed, solo is not; ops (2) stays a valid target
    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock(), agent_view_code="dev")

    assert count == 2
    assert client.list_delta.call_count == 1  # only the shared group polled (solo excluded)
    targets = {c.kwargs["agent_view_id"] for c in pub.publish_admitted_mail.call_args_list}
    assert targets == {1, 2}  # ops (2) still a target despite filtering on dev


# ---- Routed mode: a UPN shared by >=2 views is polled once and split by sender ----


def _admit_by_sender(message_id, *, sender_email=None, **kw):
    """Stand-in for OutlookPublisher.admit_mail: admit everyone, normalize the sender."""
    return OutlookAdmission(sender=(sender_email or "").strip().lower())


def _decision(agent_view_id, *, ambiguous=False):
    return SimpleNamespace(agent_view_id=agent_view_id, ambiguous=ambiguous)


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_shared_mailbox_routes_each_sender_to_its_view(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # Two views share one UPN -> ROUTED mode: poll once (by lowest-id poll_owner), route each
    # message by normalized sender, publish to the resolved in-group target with the TARGET view's
    # job priority. "Lowest id wins" is GONE.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"},
        {"id": "s2", "from": {"address": "boss@x.com"}, "dmarc": "pass"},
    ]}
    client, pub = _patch_env({1: _cfg(mailbox="shared@x.com"), 2: _cfg(mailbox="shared@x.com")},
                             lambda top, *, agent_view_id, cursors: shared)
    pub.admit_mail.side_effect = _admit_by_sender
    pub.publish_admitted_mail.return_value = True
    route = {"sklep@x.com": 1, "boss@x.com": 2}
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(route[ctx.identity_value])
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 2
    # polled exactly once, by the lowest-id member (poll_owner), not per-view
    assert client.list_delta.call_count == 1
    assert client.list_delta.call_args.kwargs["agent_view_id"] == 1
    # each message routed to the sender's view; job priority comes from the TARGET view
    routed = {c.args[1]: (c.kwargs["agent_view_id"], c.kwargs["priority"])
              for c in pub.publish_admitted_mail.call_args_list}
    assert routed == {"s1": (1, 51), "s2": (2, 52)}
    # direct-mode publish_mail is never used in routed mode
    pub.publish_mail.assert_not_called()
    # single shared cursor advanced once
    assert mock_save.call_count == 1
    assert mock_save.call_args.args[1:] == ("shared@x.com", "L")


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_no_match_ambiguous_and_outside_group_advance_without_hold(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # Deterministic non-deliverable verdicts (no match / ambiguous / target outside the group) must
    # NOT create a job AND must NOT hold the cursor — they never change on re-fetch.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "nomatch", "from": {"address": "nomatch@x.com"}, "dmarc": "pass"},
        {"id": "ambig", "from": {"address": "ambig@x.com"}, "dmarc": "pass"},
        {"id": "outside", "from": {"address": "outside@x.com"}, "dmarc": "pass"},
    ]}
    client, pub = _patch_env({1: _cfg(mailbox="shared@x.com"), 2: _cfg(mailbox="shared@x.com")},
                             lambda top, *, agent_view_id, cursors: shared)
    pub.admit_mail.side_effect = _admit_by_sender
    pub.publish_admitted_mail.return_value = True

    def _route(conn, ctx, **kw):
        v = ctx.identity_value
        if v == "nomatch@x.com":
            return None
        if v == "ambig@x.com":
            return _decision(1, ambiguous=True)
        return _decision(99)  # 99 is not a member of the group -> target outside group

    mock_resolve.side_effect = _route
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 0
    pub.publish_admitted_mail.assert_not_called()
    # cursor STILL advances (deterministic drops never hold)
    assert mock_save.call_count == 1
    assert mock_save.call_args.args[1:] == ("shared@x.com", "L")


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_router_exception_holds_cursor(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # A router/DB exception is TRANSIENT -> hold the cursor (re-fetched next poll), never memoized.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="shared@x.com"), 2: _cfg(mailbox="shared@x.com")},
                             lambda top, *, agent_view_id, cursors: shared)
    pub.admit_mail.side_effect = _admit_by_sender
    mock_resolve.side_effect = RuntimeError("router db blip")
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    mock_save.assert_not_called()  # held


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[])  # empty active binding set
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view", return_value=None)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_zero_bindings_warns_only_on_empty_active_set(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="shared@x.com"), 2: _cfg(mailbox="shared@x.com")},
                             lambda top, *, agent_view_id, cursors: shared)
    pub.admit_mail.side_effect = _admit_by_sender
    MockClient.return_value = client
    MockPub.return_value = pub
    logger = MagicMock()

    publish_all_views(object(), MagicMock(), "http://tb:3001", logger)

    assert any("no active" in str(c).lower() and "outlook_sender" in str(c)
               for c in logger.warning.call_args_list)


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_memoizes_per_unique_sender(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # Backlog/resync fan-out: N messages from K unique senders (senders repeated). resolve_agent_view
    # and resolve_publish_priority each run at most K times (per-sender memoization) — independent of N.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    senders = ["sklep@x.com", "boss@x.com", "sklep@x.com", "boss@x.com", "sklep@x.com"]  # N=5, K=2
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": f"m{i}", "from": {"address": s}, "dmarc": "pass"} for i, s in enumerate(senders)]}
    client, pub = _patch_env({1: _cfg(mailbox="shared@x.com"), 2: _cfg(mailbox="shared@x.com")},
                             lambda top, *, agent_view_id, cursors: shared)
    pub.admit_mail.side_effect = _admit_by_sender
    pub.publish_admitted_mail.return_value = True
    route = {"sklep@x.com": 1, "boss@x.com": 2}
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(route[ctx.identity_value])
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 5  # every message published (all 5 route to a valid target)
    assert mock_resolve.call_count == 2  # K unique senders, not N=5
    assert mock_prio.call_count == 2


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_security_gate_short_circuits_router(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # SECURITY ORDER: a message rejected by admit_mail (off-allowlist / DMARC fail / not activated)
    # never reaches the router — resolve_agent_view is NOT called; the cursor still advances.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "blocked", "from": {"address": "stranger@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="shared@x.com"), 2: _cfg(mailbox="shared@x.com")},
                             lambda top, *, agent_view_id, cursors: shared)
    pub.admit_mail.return_value = None  # gate rejects
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 0
    mock_resolve.assert_not_called()  # router never consulted for a rejected message
    pub.publish_admitted_mail.assert_not_called()
    assert mock_save.call_count == 1  # deterministic drop -> advance


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_publish_error_holds_cursor(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # A publish exception (transient DB blip) holds the cursor for a re-fetch next poll.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="shared@x.com"), 2: _cfg(mailbox="shared@x.com")},
                             lambda top, *, agent_view_id, cursors: shared)
    pub.admit_mail.side_effect = _admit_by_sender
    pub.publish_admitted_mail.side_effect = RuntimeError("db blip")
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(1)
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    mock_save.assert_not_called()  # held


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_mailbox_mismatch_holds_cursor(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    # The toolbox-resolved mailbox disagrees with the configured UPN (a resolution drift) -> hold.
    mock_gaav.return_value = _views((1, "dev"))
    resp = {"mailbox": "OTHER@x.com", "deltaLink": "L",
            "messages": [{"id": "d1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com")}, lambda top, *, agent_view_id, cursors: resp)
    MockClient.return_value = client
    MockPub.return_value = pub
    logger = MagicMock()

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", logger)

    assert count == 0
    pub.publish_mail.assert_not_called()
    mock_save.assert_not_called()  # held (do not publish against the wrong cursor)
    assert any("mismatch" in str(c).lower() for c in logger.warning.call_args_list)


@pytest.mark.parametrize("field,val_a,val_b", [
    ("activation_modes", "direct", "direct,mention"),
    ("summon_token", "@agento", "@bot"),
    ("direct_requires_sole_recipient", "1", "0"),
    ("mailbox_aliases", "", "team@x.com"),
    ("allow_bot_collaboration", "0", "1"),
])
@patch(f"{P}.get_event_manager")
@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_divergent_activation_field_still_stalls_and_dispatches_policy_divergence(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save, mock_em,
    field, val_a, val_b,
):
    # IDENTICAL allowed_senders but a DIFFERENT mailbox-level activation field -> still a divergence
    # for EVERY one of the five _MAILBOX_POLICY_FIELDS (allowed_senders is NOT in that set): group NOT
    # polled, policy_divergence dispatched.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", **{field: val_a}),
         2: _cfg(mailbox="shared@x.com", **{field: val_b})},
        lambda top, *, agent_view_id, cursors: {"mailbox": "shared@x.com", "deltaLink": "L", "messages": []},
    )
    MockClient.return_value = client
    MockPub.return_value = pub
    logger = MagicMock()

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", logger)

    assert count == 0
    client.list_delta.assert_not_called()  # skipped before polling
    mock_save.assert_not_called()
    mock_resolve.assert_not_called()
    assert any("divergent" in str(c).lower() for c in logger.error.call_args_list)
    evt = _one_stall_event(mock_em)
    assert evt.reason == "policy_divergence" and evt.mailbox == "shared@x.com"


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_unconfigured_mailbox_is_skipped(MockClient, MockPub, mock_gaav, mock_load, mock_save):
    mock_gaav.return_value = _views((1, "dev"))
    client, pub = _patch_env({1: _cfg()},
                             lambda top, *, agent_view_id, cursors: {"mailbox": None, "deltaLink": "L", "messages": [{"id": "x"}]})
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 0
    pub.publish_mail.assert_not_called()
    mock_save.assert_not_called()


@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_agent_views", return_value=[])
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_no_active_views_is_a_clean_noop(MockClient, MockPub, mock_gaav, mock_load):
    client = MagicMock()
    MockClient.return_value = client
    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())
    assert count == 0
    MockPub.return_value.publish_mail.assert_not_called()
    client.close.assert_called_once()


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_per_view_error_logs_and_continues(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))

    def side(top, *, agent_view_id, cursors):
        if agent_view_id == 1:
            raise RuntimeError("toolbox down for view 1")
        return {"mailbox": "ops@x.com", "deltaLink": "L", "messages": [{"id": "o1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}

    client, pub = _patch_env({1: _cfg(mailbox="v1@x.com"), 2: _cfg(mailbox="ops@x.com")}, side)
    MockClient.return_value = client
    MockPub.return_value = pub
    logger = MagicMock()

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", logger)

    assert count == 1
    logger.exception.assert_called()
    client.close.assert_called_once()


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_agent_view_code_filter_runs_one_view_only(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    client, pub = _patch_env({1: _cfg(mailbox="v1@x.com"), 2: _cfg(mailbox="v2@x.com")},
                             lambda top, *, agent_view_id, cursors: {"mailbox": f"v{agent_view_id}@x.com", "deltaLink": "L",
                                                                     "messages": [{"id": "m", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]})
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock(), agent_view_code="ops")

    polled = [c.kwargs["agent_view_id"] for c in client.list_delta.call_args_list]
    assert polled == [2]


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_cursor_advanced_only_after_publish(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    mock_gaav.return_value = _views((1, "dev"))
    resp = {"mailbox": "dev@x.com", "deltaLink": "L-NEW",
            "messages": [{"id": "d1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com")}, lambda top, *, agent_view_id, cursors: resp)
    MockClient.return_value = client
    MockPub.return_value = pub
    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())
    mock_save.assert_called_once()
    assert mock_save.call_args.args[1:] == ("dev@x.com", "L-NEW")


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_temperror_advances_cursor_not_held(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    # temperror is a FROZEN per-message verdict (immutable receipt-time header), not a transient
    # signal: it must NOT pin the cursor. The message is not published (gate rejects non-pass), but
    # the cursor advances so it can't re-clog the window / grow the delta without bound.
    mock_gaav.return_value = _views((1, "dev"))
    resp = {"mailbox": "dev@x.com", "deltaLink": "L-NEW",
            "messages": [{"id": "t1", "from": {"address": "sklep@x.com"}, "dmarc": "temperror"}]}
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com")}, lambda top, *, agent_view_id, cursors: resp)
    pub.publish_mail.return_value = False  # temperror is not published by the gate
    MockClient.return_value = client
    MockPub.return_value = pub
    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())
    mock_save.assert_called_once()  # advanced: not pinned forever
    assert mock_save.call_args.args[1:] == ("dev@x.com", "L-NEW")


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_publish_exception_holds_cursor(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    # A genuinely transient failure (publish_mail raises, e.g. a DB blip) DOES hold the cursor so the
    # batch is re-fetched next poll.
    mock_gaav.return_value = _views((1, "dev"))
    resp = {"mailbox": "dev@x.com", "deltaLink": "L-NEW",
            "messages": [{"id": "e1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com")}, lambda top, *, agent_view_id, cursors: resp)
    pub.publish_mail.side_effect = RuntimeError("db blip")
    MockClient.return_value = client
    MockPub.return_value = pub
    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())
    mock_save.assert_not_called()  # held: re-fetched next poll


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors")
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_loaded_cursors_passed_to_list_delta(MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save):
    mock_load.return_value = {"dev@x.com": "PREV"}
    mock_gaav.return_value = _views((1, "dev"))
    seen = {}

    def side(top, *, agent_view_id, cursors):
        seen["cursors"] = cursors
        return {"mailbox": "dev@x.com", "deltaLink": "L", "messages": []}

    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com")}, side)
    MockClient.return_value = client
    MockPub.return_value = pub
    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())
    assert seen["cursors"] == {"dev@x.com": "PREV"}


# --- mailbox_stall_after ops event (silent-mailbox misconfigurations) --------------------------
# A shared mailbox that stops delivering mail on a misconfiguration is otherwise visible only in a
# log line; each of the three conditions must dispatch mailbox_stall_after so app_monitor can email
# ops (mirrors the security_breach_after → SecurityBreachAlertObserver pattern).


def _one_stall_event(mock_em):
    """Return the single MailboxStalledEvent dispatched under 'mailbox_stall_after'."""
    calls = [
        c.args for c in mock_em.return_value.dispatch.call_args_list
        if c.args and c.args[0] == "mailbox_stall_after"
    ]
    assert len(calls) == 1, f"expected exactly one mailbox_stall_after dispatch, got {calls}"
    return calls[0][1]


@patch(f"{P}.get_event_manager")
@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_divergent_allowed_senders_does_not_stall(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save, mock_em,
):
    # allowed_senders is now PER-VIEW: two members with DIFFERENT allow-lists must NOT stall the
    # group (no policy_divergence) — the group is polled and mail is published normally.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "a@dev.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", allowed="*@dev.com"),
         2: _cfg(mailbox="shared@x.com", allowed="*@ops.com")},  # divergent allowed_senders — OK now
        lambda top, *, agent_view_id, cursors: shared,
    )
    pub.admit_mail.side_effect = _admit_union
    pub.sender_allowed.side_effect = _real_sender_allowed
    pub.publish_admitted_mail.return_value = True
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(1)  # a@dev.com -> dev (id 1)
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 1  # polled + published, no stall
    assert client.list_delta.call_count == 1
    assert not any(
        c.args and c.args[0] == "mailbox_stall_after" and c.args[1].reason == "policy_divergence"
        for c in mock_em.return_value.dispatch.call_args_list
    )


@patch(f"{P}.get_event_manager")
@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_upn_mismatch_dispatches_mailbox_stall_event(
    MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save, mock_em,
):
    mock_gaav.return_value = _views((1, "dev"))
    resp = {"mailbox": "OTHER@x.com", "deltaLink": "L",
            "messages": [{"id": "d1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com")}, lambda top, *, agent_view_id, cursors: resp)
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    evt = _one_stall_event(mock_em)
    assert evt.channel == "outlook"
    assert evt.mailbox == "dev@x.com"  # the configured UPN we intended to poll
    assert evt.reason == "upn_mismatch"


@patch(f"{P}.get_event_manager")
@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[])  # empty active binding set
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view", return_value=None)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_zero_bindings_dispatches_mailbox_stall_event(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save, mock_em,
):
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "sklep@x.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env({1: _cfg(mailbox="shared@x.com"), 2: _cfg(mailbox="shared@x.com")},
                             lambda top, *, agent_view_id, cursors: shared)
    pub.admit_mail.side_effect = _admit_by_sender
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    evt = _one_stall_event(mock_em)
    assert evt.channel == "outlook"
    assert evt.mailbox == "shared@x.com"
    assert evt.reason == "no_bindings"


# --- Route-first per-view authorization: union pre-filter + post-route refinement ---------------


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_admit_receives_exact_sorted_union(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # admit_mail is called with the exact de-duped, sorted UNION of every member's allow-list.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "a@dev.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", allowed="*@dev.com"),
         2: _cfg(mailbox="shared@x.com", allowed="b@ops.com,*@ops.com")},
        lambda top, *, agent_view_id, cursors: shared,
    )
    seen = []

    def _admit(mid, **kw):
        seen.append(kw["allowed_senders"])
        return _admit_union(mid, **kw)

    pub.admit_mail.side_effect = _admit
    pub.sender_allowed.side_effect = _real_sender_allowed
    pub.publish_admitted_mail.return_value = True
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(1)
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert seen and all(a == ["*@dev.com", "*@ops.com", "b@ops.com"] for a in seen)


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_admit_receives_auto_reply_flag(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # Routed mode: the toolbox-derived auto_reply boolean must reach admit_mail.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "a@dev.com"}, "dmarc": "fail", "auto_reply": True}]}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", allowed="*@dev.com"),
         2: _cfg(mailbox="shared@x.com", allowed="*@ops.com")},
        lambda top, *, agent_view_id, cursors: shared,
    )
    seen = []

    def _admit(mid, **kw):
        seen.append(kw.get("auto_reply"))
        return _admit_union(mid, **kw)

    pub.admit_mail.side_effect = _admit
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(1)
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert seen == [True]


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_shared_mailbox_with_divergent_allowlists_does_not_stall_and_routes_per_view(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # dev trusts *@dev.com, ops trusts *@ops.com; each sender lands on its own view.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "a@dev.com"}, "dmarc": "pass"},
        {"id": "s2", "from": {"address": "b@ops.com"}, "dmarc": "pass"},
    ]}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", allowed="*@dev.com"),
         2: _cfg(mailbox="shared@x.com", allowed="*@ops.com")},
        lambda top, *, agent_view_id, cursors: shared,
    )
    pub.admit_mail.side_effect = _admit_union
    pub.sender_allowed.side_effect = _real_sender_allowed
    pub.publish_admitted_mail.return_value = True
    route = {"a@dev.com": 1, "b@ops.com": 2}
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(route[ctx.identity_value])
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 2
    assert client.list_delta.call_count == 1
    routed = {c.args[1]: c.kwargs["agent_view_id"] for c in pub.publish_admitted_mail.call_args_list}
    assert routed == {"s1": 1, "s2": 2}
    assert mock_save.called


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_sender_outside_union_is_dropped_before_routing(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # A sender in NO member's allow-list is rejected by the union pre-filter BEFORE any routing work.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "stranger@evil.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", allowed="*@dev.com"),
         2: _cfg(mailbox="shared@x.com", allowed="*@ops.com")},
        lambda top, *, agent_view_id, cursors: shared,
    )
    pub.admit_mail.side_effect = _admit_union
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 0
    assert mock_resolve.call_count == 0  # union pre-filter short-circuits before routing
    pub.publish_admitted_mail.assert_not_called()
    assert mock_save.called  # deterministic drop advances the cursor


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_post_route_refinement_drops_when_target_view_allowlist_rejects(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # d@dev.com passes the UNION (dev trusts it) but its binding routes it to OPS, whose own list
    # rejects it -> deterministic drop (no job), cursor advances, no hold.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "d@dev.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", allowed="*@dev.com"),
         2: _cfg(mailbox="shared@x.com", allowed="*@ops.com")},
        lambda top, *, agent_view_id, cursors: shared,
    )
    pub.admit_mail.side_effect = _admit_union
    pub.sender_allowed.side_effect = _real_sender_allowed
    pub.publish_admitted_mail.return_value = True
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(2)  # routes to ops
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 0
    pub.publish_admitted_mail.assert_not_called()
    assert mock_save.called  # advances (deterministic drop, no hold)


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_union_empty_across_group_drops_everyone_fail_closed(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save,
):
    # Every member's allowed_senders empty -> union == [] -> the real matcher rejects all -> no jobs.
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "a@dev.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", allowed=""),
         2: _cfg(mailbox="shared@x.com", allowed="")},
        lambda top, *, agent_view_id, cursors: shared,
    )
    pub.admit_mail.side_effect = _admit_union
    MockClient.return_value = client
    MockPub.return_value = pub

    count = publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert count == 0
    assert mock_resolve.call_count == 0
    assert mock_save.called


# --- Observability: per-poll drop summary event + log, effective-policy startup log ------------


def _drop_scenario_env():
    """Two messages in one routed poll: one unroutable (no binding) + one per-view-allowlist reject."""
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "u1", "from": {"address": "nobody@dev.com"}, "dmarc": "pass"},  # admitted, route None
        {"id": "p1", "from": {"address": "e@dev.com"}, "dmarc": "pass"},       # admitted, ops rejects
    ]}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", allowed="*@dev.com"),
         2: _cfg(mailbox="shared@x.com", allowed="*@ops.com")},
        lambda top, *, agent_view_id, cursors: shared,
    )
    pub.admit_mail.side_effect = _admit_union
    pub.sender_allowed.side_effect = _real_sender_allowed
    pub.publish_admitted_mail.return_value = True
    return client, pub


def _drop_route(conn, ctx, **kw):
    return None if ctx.identity_value == "nobody@dev.com" else _decision(2)  # p1 -> ops (rejected)


@patch(f"{P}.get_event_manager")
@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view", side_effect=_drop_route)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_drops_dispatch_summary_event_once_per_poll(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save, mock_em,
):
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    client, pub = _drop_scenario_env()
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    drop = [c.args[1] for c in mock_em.return_value.dispatch.call_args_list
            if c.args and c.args[0] == "inbound_route_drop_after"]
    assert len(drop) == 1
    assert drop[0].channel == "outlook"
    assert drop[0].mailbox == "shared@x.com"
    assert drop[0].unroutable == 1
    assert drop[0].per_view_allowlist == 1
    assert drop[0].ambiguous == 0


@patch(f"{P}.get_event_manager")
@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view")
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_no_drop_event_when_all_routed_cleanly(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save, mock_em,
):
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    shared = {"mailbox": "shared@x.com", "deltaLink": "L", "messages": [
        {"id": "s1", "from": {"address": "a@dev.com"}, "dmarc": "pass"}]}
    client, pub = _patch_env(
        {1: _cfg(mailbox="shared@x.com", allowed="*@dev.com"),
         2: _cfg(mailbox="shared@x.com", allowed="*@ops.com")},
        lambda top, *, agent_view_id, cursors: shared,
    )
    pub.admit_mail.side_effect = _admit_union
    pub.sender_allowed.side_effect = _real_sender_allowed
    pub.publish_admitted_mail.return_value = True
    mock_resolve.side_effect = lambda conn, ctx, **kw: _decision(1)
    MockClient.return_value = client
    MockPub.return_value = pub

    publish_all_views(object(), MagicMock(), "http://tb:3001", MagicMock())

    assert not any(c.args and c.args[0] == "inbound_route_drop_after"
                   for c in mock_em.return_value.dispatch.call_args_list)


@patch(f"{P}.get_event_manager")
@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.get_active_identities_for_type", return_value=[object()])
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.resolve_agent_view", side_effect=_drop_route)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_routed_poll_emits_drop_summary_log(
    MockClient, MockPub, mock_gaav, mock_resolve, mock_prio, mock_bindings, mock_load, mock_save, mock_em,
):
    mock_gaav.return_value = _views((1, "dev"), (2, "ops"))
    client, pub = _drop_scenario_env()
    MockClient.return_value = client
    MockPub.return_value = pub
    logger = MagicMock()

    publish_all_views(object(), MagicMock(), "http://tb:3001", logger)

    assert any("routed-poll drop summary" in str(c).lower() for c in logger.info.call_args_list)


@patch(f"{P}.save_cursor")
@patch(f"{P}.load_cursors", return_value={})
@patch(f"{P}.resolve_publish_priority", side_effect=lambda conn, av_id: 50 + av_id)
@patch(f"{P}.ScopedConfigService", _FakeScoped)
@patch(f"{P}.get_active_agent_views")
@patch(f"{P}.OutlookPublisher")
@patch(f"{P}.OutlookToolboxClient")
def test_publisher_logs_effective_policy_per_view_at_start(
    MockClient, MockPub, mock_gaav, mock_prio, mock_load, mock_save,
):
    # One effective-policy info line per enabled view (count only — never the raw patterns).
    mock_gaav.return_value = _views((1, "dev"))
    resp = {"mailbox": "dev@x.com", "deltaLink": "L", "messages": []}
    client, pub = _patch_env({1: _cfg(mailbox="dev@x.com", allowed="*@dev.com")},
                             lambda top, *, agent_view_id, cursors: resp)
    MockClient.return_value = client
    MockPub.return_value = pub
    logger = MagicMock()

    publish_all_views(object(), MagicMock(), "http://tb:3001", logger)

    recs = [c for c in logger.info.call_args_list if "effective outlook policy" in str(c).lower()]
    assert recs
    # count only — a raw allow-list pattern (e.g. "*@dev.com") must never appear in the log call
    assert not any("*@dev.com" in str(c) for c in recs)
