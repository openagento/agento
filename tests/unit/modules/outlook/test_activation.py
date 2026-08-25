from agento.modules.outlook.src.activation import (
    Decision,
    decide,
    has_summon_token,
    is_direct_addressed,
    normalize_subject,
)
from agento.modules.outlook.src.config import OutlookConfig

MAILBOX = "agent@example.com"


def _cfg(*, modes="direct,mention", sole=True, collab=False, token="@agento", aliases=""):
    return OutlookConfig.from_dict(
        {
            "activation_modes": modes,
            "direct_requires_sole_recipient": "1" if sole else "0",
            "allow_bot_collaboration": "1" if collab else "0",
            "summon_token": token,
            "mailbox_aliases": aliases,
        }
    )


def _to(*addrs):
    return [{"name": "", "address": a} for a in addrs]


# ---- normalize_subject ----

def test_normalize_subject_strips_reply_prefixes_and_lowercases():
    assert normalize_subject("Re: Faktura") == "faktura"
    assert normalize_subject("FWD: Re: Pytanie") == "pytanie"
    assert normalize_subject("Re[2]: Report") == "report"
    assert normalize_subject("  RE:  Spaced  ") == "spaced"


def test_normalize_subject_handles_none_and_plain():
    assert normalize_subject(None) == ""
    assert normalize_subject("Plain subject") == "plain subject"


# ---- has_summon_token ----

def test_has_summon_token_case_insensitive_word_boundary():
    assert has_summon_token("Hej @Agento pomóż", "@agento") is True
    assert has_summon_token("Start @agento", "@agento") is True
    assert has_summon_token("@agento", "@agento") is True


def test_has_summon_token_absent_or_embedded_does_not_match():
    assert has_summon_token("nothing here", "@agento") is False
    # embedded in a longer token -> not a summon
    assert has_summon_token("@agentozord rules", "@agento") is False
    # inside an email address -> preceding word char blocks the match
    assert has_summon_token("write to user@agento.com please", "@agento") is False


def test_has_summon_token_empty_token_or_text():
    assert has_summon_token("anything", "") is False
    assert has_summon_token(None, "@agento") is False


# ---- is_direct_addressed ----

def test_is_direct_addressed_sole_recipient():
    match = {MAILBOX}
    assert is_direct_addressed(match, _to(MAILBOX), [], require_sole=True) is True


def test_is_direct_addressed_not_sole_when_other_recipient_present():
    match = {MAILBOX}
    assert is_direct_addressed(match, _to(MAILBOX, "human@example.com"), [], require_sole=True) is False
    # relaxed sole -> still direct because the mailbox is addressed
    assert is_direct_addressed(match, _to(MAILBOX, "human@example.com"), [], require_sole=False) is True


def test_is_direct_addressed_cc_counts_toward_recipients():
    match = {MAILBOX}
    # mailbox on cc but a human on to -> not sole
    assert is_direct_addressed(match, _to("human@example.com"), _to(MAILBOX), require_sole=True) is False
    # mailbox is the only recipient across to+cc
    assert is_direct_addressed(match, [], _to(MAILBOX), require_sole=True) is True


def test_is_direct_addressed_alias_match():
    match = {MAILBOX, "support@example.com"}
    assert is_direct_addressed(match, _to("SUPPORT@example.com"), [], require_sole=True) is True


def test_is_direct_addressed_no_recipients_is_false():
    assert is_direct_addressed({MAILBOX}, [], [], require_sole=True) is False
    assert is_direct_addressed({MAILBOX}, [], [], require_sole=False) is False


# ---- decide: human (no marker) ----

def test_decide_direct_sole_recipient_responds():
    d = decide(
        agent_authored=False, cfg=_cfg(), mailbox=MAILBOX, aliases=[],
        to_addrs=_to(MAILBOX), cc_addrs=[], subject="Pytanie", body_preview="treść",
    )
    assert d == Decision(can_respond=True, reason="direct")


def test_decide_cc_only_no_mention_stays_silent():
    d = decide(
        agent_authored=False, cfg=_cfg(), mailbox=MAILBOX, aliases=[],
        to_addrs=_to("human@example.com"), cc_addrs=_to(MAILBOX),
        subject="Pytanie", body_preview="brak wołania",
    )
    assert d.can_respond is False
    assert d.reason == "no_active_mode"


def test_decide_summon_in_subject_responds():
    d = decide(
        agent_authored=False, cfg=_cfg(), mailbox=MAILBOX, aliases=[],
        to_addrs=_to("human@example.com", "other@example.com"), cc_addrs=[],
        subject="Re: @agento pomóż z tym", body_preview="",
    )
    assert d == Decision(can_respond=True, reason="mention")


def test_decide_summon_in_body_preview_responds():
    d = decide(
        agent_authored=False, cfg=_cfg(), mailbox=MAILBOX, aliases=[],
        to_addrs=_to("human@example.com", "other@example.com"), cc_addrs=[],
        subject="zwykły temat", body_preview="cześć @agento, zrób to",
    )
    assert d == Decision(can_respond=True, reason="mention")


def test_decide_neither_direct_nor_mention_stays_silent():
    d = decide(
        agent_authored=False, cfg=_cfg(), mailbox=MAILBOX, aliases=[],
        to_addrs=_to("human@example.com", "other@example.com"), cc_addrs=[],
        subject="zwykły temat", body_preview="brak wołania",
    )
    assert d.can_respond is False


def test_decide_alias_direct_addressed_responds():
    cfg = _cfg(aliases="support@example.com, sales@example.com")
    d = decide(
        agent_authored=False, cfg=cfg, mailbox=MAILBOX, aliases=cfg.mailbox_aliases_list,
        to_addrs=_to("support@example.com"), cc_addrs=[], subject="temat", body_preview="",
    )
    assert d == Decision(can_respond=True, reason="direct")


def test_decide_relaxed_sole_recipient_direct_on_group():
    cfg = _cfg(sole=False)
    d = decide(
        agent_authored=False, cfg=cfg, mailbox=MAILBOX, aliases=[],
        to_addrs=_to(MAILBOX, "human@example.com"), cc_addrs=[], subject="t", body_preview="",
    )
    assert d == Decision(can_respond=True, reason="direct")


def test_decide_mode_disabled_does_not_fire():
    # only "mention" active -> a direct-addressed mail with no token stays silent
    cfg = _cfg(modes="mention")
    d = decide(
        agent_authored=False, cfg=cfg, mailbox=MAILBOX, aliases=[],
        to_addrs=_to(MAILBOX), cc_addrs=[], subject="temat", body_preview="",
    )
    assert d.can_respond is False


# ---- decide: loop marker (agent_authored) ----

def test_decide_agent_authored_with_humans_default_hard_suppress():
    # The key regression: an agent-authored mail addressed straight to the mailbox (would be
    # direct) must NOT spawn a job under the default config, even with humans on the thread.
    d = decide(
        agent_authored=True, cfg=_cfg(), mailbox=MAILBOX, aliases=[],
        to_addrs=_to(MAILBOX, "human@example.com"), cc_addrs=[],
        subject="@agento kontynuuj", body_preview="@agento kontynuuj",
    )
    assert d == Decision(can_respond=False, reason="agent_authored")


def test_decide_agent_authored_with_collaboration_follows_mode_direct():
    cfg = _cfg(collab=True)
    d = decide(
        agent_authored=True, cfg=cfg, mailbox=MAILBOX, aliases=[],
        to_addrs=_to(MAILBOX), cc_addrs=[], subject="t", body_preview="",
    )
    assert d == Decision(can_respond=True, reason="direct")


def test_decide_agent_authored_with_collaboration_still_silent_without_mode():
    cfg = _cfg(collab=True)
    d = decide(
        agent_authored=True, cfg=cfg, mailbox=MAILBOX, aliases=[],
        to_addrs=_to("human@example.com", "other@example.com"), cc_addrs=[],
        subject="zwykły temat", body_preview="brak wołania",
    )
    assert d.can_respond is False
    assert d.reason == "no_active_mode"


# ---- an emptied setting must actually disable, not silently re-enable ----

def test_decide_with_activation_modes_cleared_never_responds():
    """`config:set outlook/activation_modes ""` is how an operator mutes a mailbox."""
    d = decide(
        agent_authored=False, cfg=_cfg(modes=""), mailbox=MAILBOX, aliases=[],
        to_addrs=_to(MAILBOX), cc_addrs=[], subject="@agento pomóż", body_preview="@agento",
    )
    assert d == Decision(can_respond=False, reason="no_active_mode")


def test_decide_with_summon_token_cleared_disables_mention_but_keeps_direct():
    cfg = _cfg(token="")
    silent = decide(
        agent_authored=False, cfg=cfg, mailbox=MAILBOX, aliases=[],
        to_addrs=_to("human@example.com"), cc_addrs=_to(MAILBOX),
        subject="@agento pomóż", body_preview="@agento",
    )
    assert silent.reason == "no_active_mode"
    direct = decide(
        agent_authored=False, cfg=cfg, mailbox=MAILBOX, aliases=[],
        to_addrs=_to(MAILBOX), cc_addrs=[], subject="Pytanie", body_preview="treść",
    )
    assert direct.can_respond is True
