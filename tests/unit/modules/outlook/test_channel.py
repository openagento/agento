import hashlib
import logging
from unittest.mock import MagicMock, patch

from agento.framework.channels.base import Channel, PromptFragments
from agento.framework.job_models import RequesterTrust
from agento.modules.outlook.src.channel import (
    OutlookAdmission,
    OutlookChannel,
    OutlookPublisher,
    _build_reference_id,
    _slugify,
)

WHITELIST = ["sklep@mycompanystudio.com", "ops@mycompany.com", "*@partner.com"]


# ---- _slugify: ASCII, log-safe, deterministic subject slug ----

def test_slugify_basic_subject():
    assert _slugify("Re: Test 1 - skills & tools") == "re-test-1-skills-tools"


def test_slugify_transliterates_polish_diacritics():
    assert _slugify("Zażółć gęślą jaźń") == "zazolc-gesla-jazn"


def test_slugify_handles_l_with_stroke():
    # NFKD does NOT decompose ł/Ł — must be special-cased before normalize.
    assert _slugify("Łódź") == "lodz"


def test_slugify_empty_or_punctuation_only_falls_back_to_mail():
    assert _slugify("") == "mail"
    assert _slugify(None) == "mail"
    assert _slugify("   ") == "mail"
    assert _slugify("!!! ??? ...") == "mail"


def test_slugify_caps_length_and_restrips_trailing_dash():
    out = _slugify("word " * 40, max_len=20)
    assert len(out) <= 20
    assert not out.endswith("-")
    assert not out.startswith("-")


# ---- _build_reference_id: subjectSlug + "::" + messageId, messageId sacred ----

def test_build_reference_id_compounds_slug_and_message_id():
    assert _build_reference_id("AAMk=", "Re: Faktura") == "re-faktura::AAMk="


def test_build_reference_id_bare_when_no_subject():
    assert _build_reference_id("AAMk=", None) == "AAMk="
    assert _build_reference_id("AAMk=", "") == "AAMk="
    assert _build_reference_id("AAMk=", "   ") == "AAMk="


def test_build_reference_id_never_truncates_message_id_truncates_slug():
    long_id = "A" * 500
    ref = _build_reference_id(long_id, "some subject here", max_total=512)
    msg_id = ref.rsplit("::", 1)[-1]
    assert msg_id == long_id  # message_id survives intact
    assert len(ref) <= 512


def test_build_reference_id_falls_back_to_bare_when_no_room_for_slug():
    # message_id alone consumes the whole budget — store bare id, never truncate it.
    long_id = "A" * 511
    assert _build_reference_id(long_id, "subject", max_total=512) == long_id


def test_build_reference_id_round_trips_via_rsplit():
    ref = _build_reference_id("AAMkAG/x+y=", "Pytanie o raport")
    assert ref.rsplit("::", 1)[-1] == "AAMkAG/x+y="


def test_slugify_caps_pathological_1000_char_subject():
    out = _slugify("z" * 1000)
    assert len(out) <= 60  # hard cap regardless of input length


def test_build_reference_id_survives_1000_char_subject_with_realistic_id():
    # ACC: a 1000-char subject must NOT blow up — slug is capped/truncated, message_id stays intact,
    # and the whole thing fits the VARCHAR(512) column.
    msg_id = "AAMk" + "B" * 216  # 220 chars, realistic long Graph id
    ref = _build_reference_id(msg_id, "Pytanie " * 200)  # 1600-char subject
    assert len(ref) <= 512
    assert ref.rsplit("::", 1)[-1] == msg_id  # message_id never truncated
    assert ref.split("::", 1)[0] and len(ref.split("::", 1)[0]) <= 60  # slug present but capped


def test_channel_protocol_and_name():
    ch = OutlookChannel()
    assert isinstance(ch, Channel)
    assert ch.name == "outlook"


def test_prompt_fragments_reference_tools_and_id():
    f = OutlookChannel().get_prompt_fragments("AAMkAG=")
    assert isinstance(f, PromptFragments)
    assert "outlook_get_message" in f.read_context
    assert "AAMkAG=" in f.read_context
    assert "outlook_reply" in f.respond
    assert "HTML" in f.respond  # reply body must be formatted as HTML, not plain text
    assert "outlook_mark_processed" in f.transition_done


def test_followup_fragments_carry_instructions():
    f = OutlookChannel().get_followup_fragments("AAMkAG=", "sprawdź raport")
    assert "sprawdź raport" in f.extra
    assert "outlook_get_message" in f.read_context
    assert "HTML" in f.respond  # follow-up replies must also be HTML


def test_prompt_fragments_provide_short_email_task_intro():
    # The opening prompt line should not repeat the (long, compound) reference_id — the channel
    # supplies a short fixed intro instead; the bare message_id still arrives via read_context.
    f = OutlookChannel().get_prompt_fragments("re-faktura::AAMkAG=")
    assert f.task_intro == "Wykonaj zadanie z wiadomości email."


def test_prompt_fragments_recover_bare_message_id_from_compound_reference():
    # reference_id stored as 'slug::messageId' — the agent prompt must show only the bare
    # message_id (the slug is cosmetic and would 404 if passed to outlook_get_message).
    f = OutlookChannel().get_prompt_fragments("re-faktura::AAMkAG=")
    assert "message_id: AAMkAG=" in f.read_context
    assert "re-faktura" not in f.read_context


def test_followup_fragments_recover_bare_message_id_from_compound_reference():
    f = OutlookChannel().get_followup_fragments("re-faktura::AAMkAG=", "sprawdź raport")
    assert "AAMkAG=" in f.read_context
    assert "re-faktura" not in f.read_context


def test_followup_fragments_provide_short_email_intro():
    f = OutlookChannel().get_followup_fragments("re-faktura::AAMkAG=", "sprawdź raport")
    assert f.followup_intro == "Kontynuuj zadanie z wiadomości email."


def test_idempotency_key_is_stable_per_message():
    assert OutlookPublisher().build_idempotency_key("m-123") == "outlook:mail:m-123"


def test_idempotency_key_fits_db_column_for_realistic_graph_id():
    # job.idempotency_key is VARCHAR(512) (migration 027). A long Graph id (>255) plus the
    # "outlook:mail:" prefix must fit — the old 255 column silently truncated it (INSERT IGNORE).
    graph_id = "AAMkAG" + "A" * 220  # 226 chars, over the old 255 only once prefixed
    key = OutlookPublisher().build_idempotency_key(graph_id)
    assert len(key) <= 512


# ---- ACC1: whitelisted + dmarc=pass -> publishes to the SUPPLIED agent_view_id/priority ----

def test_acc1_publishes_with_supplied_view_and_priority_and_domain_trust():
    p = OutlookPublisher()
    with patch("agento.modules.outlook.src.channel.publish", return_value=True) as mock_publish:
        result = p.publish_mail(
            object(), "m1", agent_view_id=7, priority=60,
            sender_email="sklep@mycompanystudio.com", dmarc="pass",
            allowed_senders=WHITELIST, logger=MagicMock(),
        )
    assert result is True
    mock_publish.assert_called_once()
    _, kwargs = mock_publish.call_args
    assert kwargs["agent_view_id"] == 7
    assert kwargs["priority"] == 60
    assert kwargs["skip_if_active"] is True
    requester = kwargs["requester"]
    assert requester.email == "sklep@mycompanystudio.com"
    assert requester.trust == RequesterTrust.DOMAIN
    assert requester.key.startswith("outlook:email:")
    assert requester.meta == {"basis": "email_from", "dmarc": "pass"}


def test_publish_mail_builds_compound_reference_id_from_subject():
    # reference_id carries a readable subject slug; idempotency_key stays the bare message_id.
    p = OutlookPublisher()
    with patch("agento.modules.outlook.src.channel.publish", return_value=True) as mock_publish:
        p.publish_mail(
            object(), "m1", agent_view_id=1, subject="Re: Faktura",
            sender_email="sklep@mycompanystudio.com", dmarc="pass",
            allowed_senders=WHITELIST, logger=MagicMock(),
        )
    args, kwargs = mock_publish.call_args
    assert kwargs["reference_id"] == "re-faktura::m1"
    assert args[3] == "outlook:mail:m1"  # idempotency_key stays bare


def test_publish_mail_bare_reference_id_when_no_subject():
    p = OutlookPublisher()
    with patch("agento.modules.outlook.src.channel.publish", return_value=True) as mock_publish:
        p.publish_mail(
            object(), "m1", agent_view_id=1,
            sender_email="sklep@mycompanystudio.com", dmarc="pass",
            allowed_senders=WHITELIST, logger=MagicMock(),
        )
    _, kwargs = mock_publish.call_args
    assert kwargs["reference_id"] == "m1"


def test_acc1_wildcard_pattern_matches():
    p = OutlookPublisher()
    with patch("agento.modules.outlook.src.channel.publish", return_value=True) as mock_publish:
        result = p.publish_mail(
            object(), "m3", agent_view_id=1, priority=50,
            sender_email="anyone@partner.com", dmarc="pass",
            allowed_senders=WHITELIST, logger=MagicMock(),
        )
    assert result is True
    mock_publish.assert_called_once()


def test_wildcard_does_not_cross_at_sign():
    p = OutlookPublisher()
    with patch("agento.modules.outlook.src.channel.publish") as mock_publish:
        result = p.publish_mail(
            object(), "m4", agent_view_id=1,
            sender_email="evil@sub.partner.com", dmarc="pass",
            allowed_senders=WHITELIST, logger=MagicMock(),
        )
    assert result is False
    mock_publish.assert_not_called()


# ---- ACC2: non-whitelisted -> NO publish, NO breach log ----

def test_acc2_non_whitelisted_sender_skipped_no_breach():
    p = OutlookPublisher()
    logger = MagicMock()
    with patch("agento.modules.outlook.src.channel.publish") as mock_publish:
        result = p.publish_mail(
            object(), "m5", agent_view_id=1,
            sender_email="test@mycompanystudio.com", dmarc="pass",
            allowed_senders=WHITELIST, logger=logger,
        )
    assert result is False
    mock_publish.assert_not_called()
    for call in logger.error.call_args_list:
        assert "SECURITY_BREACH" not in str(call)
    logger.info.assert_called()


def test_acc2_does_not_leak_full_address_only_domain():
    p = OutlookPublisher()
    logger = MagicMock()
    with patch("agento.modules.outlook.src.channel.publish"):
        p.publish_mail(
            object(), "m5b", agent_view_id=1,
            sender_email="secret-user@mycompanystudio.com", dmarc="pass",
            allowed_senders=WHITELIST, logger=logger,
        )
    blob = " ".join(str(c) for c in logger.info.call_args_list)
    assert "secret-user" not in blob


# ---- ACC3: whitelisted + dmarc!=pass -> NO publish AND SECURITY_BREACH logged ----

def test_acc3_whitelisted_sender_dmarc_fail_logs_breach_dispatches_event_no_publish():
    p = OutlookPublisher()
    logger = MagicMock()
    with patch("agento.modules.outlook.src.channel.publish") as mock_publish, \
         patch("agento.modules.outlook.src.channel.get_event_manager") as mock_em:
        result = p.publish_mail(
            object(), "AAMkAG-spoofed-message-id", agent_view_id=1,
            sender_email="sklep@mycompanystudio.com", dmarc="fail",
            allowed_senders=WHITELIST, logger=logger,
        )
    assert result is False
    mock_publish.assert_not_called()
    logger.error.assert_called_once()
    args, kwargs = logger.error.call_args
    assert "SECURITY_BREACH" in args[0]
    extra = kwargs["extra"]
    assert extra["event"] == "security_breach"
    assert extra["reason"] == "dmarc_not_pass"
    assert extra["sender"] == "sklep@mycompanystudio.com"
    assert extra["dmarc"] == "fail"
    assert "message_id" in extra
    # a probable spoof also dispatches the framework security_breach event (ops alert)
    mock_em.return_value.dispatch.assert_called_once()
    evt_name, evt = mock_em.return_value.dispatch.call_args.args
    assert evt_name == "security_breach_after"
    assert evt.channel == "outlook"
    assert evt.reason == "dmarc_not_pass"
    assert evt.sender == "sklep@mycompanystudio.com"


def test_acc3_quarantine_and_reject_are_breaches():
    p = OutlookPublisher()
    for verdict in ("quarantine", "reject"):
        logger = MagicMock()
        with patch("agento.modules.outlook.src.channel.publish") as mock_publish, \
             patch("agento.modules.outlook.src.channel.get_event_manager") as mock_em:
            result = p.publish_mail(
                object(), "m-spoof", agent_view_id=1,
                sender_email="sklep@mycompanystudio.com", dmarc=verdict,
                allowed_senders=WHITELIST, logger=logger,
            )
        assert result is False
        mock_publish.assert_not_called()
        logger.error.assert_called_once()
        mock_em.return_value.dispatch.assert_called_once()


def test_dmarc_none_or_bestguesspass_is_not_a_breach_no_publish_no_event():
    # A recordless domain (none / bestguesspass / missing verdict) is NOT a spoof: info log, no
    # SECURITY_BREACH error, no ops event, and still not published (fail-closed).
    p = OutlookPublisher()
    for verdict in (None, "none", "bestguesspass", "temperror"):
        logger = MagicMock()
        with patch("agento.modules.outlook.src.channel.publish") as mock_publish, \
             patch("agento.modules.outlook.src.channel.get_event_manager") as mock_em:
            result = p.publish_mail(
                object(), "m6", agent_view_id=1,
                sender_email="sklep@mycompanystudio.com", dmarc=verdict,
                allowed_senders=WHITELIST, logger=logger,
            )
        assert result is False
        mock_publish.assert_not_called()
        for call in logger.error.call_args_list:
            assert "SECURITY_BREACH" not in str(call)
        mock_em.return_value.dispatch.assert_not_called()
        logger.info.assert_called()


def test_empty_allowed_senders_blocks_everyone():
    p = OutlookPublisher()
    logger = MagicMock()
    for allowed in (None, [], ""):
        with patch("agento.modules.outlook.src.channel.publish") as mock_publish:
            result = p.publish_mail(
                object(), "m8", agent_view_id=1,
                sender_email="sklep@mycompanystudio.com", dmarc="pass",
                allowed_senders=allowed, logger=logger,
            )
        assert result is False
        mock_publish.assert_not_called()
    for call in logger.error.call_args_list:
        assert "SECURITY_BREACH" not in str(call)


# ---- Publisher protocol shim threads agent_view_id + priority + gate kwargs ----

def test_publish_todo_shim_threads_view_priority_and_gate_kwargs():
    p = OutlookPublisher()
    captured = {}

    def _spy(db_config, message_id, **kwargs):
        captured["message_id"] = message_id
        captured.update(kwargs)
        return True

    p.publish_mail = _spy  # type: ignore[assignment]
    result = p.publish_todo(
        object(), reference_id="m10",
        agent_view_id=3, priority=70,
        sender_email="sklep@mycompanystudio.com", dmarc="pass",
        allowed_senders=WHITELIST, logger=logging.getLogger("t"),
    )
    assert result is True
    assert captured["message_id"] == "m10"
    assert captured["agent_view_id"] == 3
    assert captured["priority"] == 70
    assert captured["sender_email"] == "sklep@mycompanystudio.com"
    assert captured["dmarc"] == "pass"
    assert captured["allowed_senders"] == WHITELIST


def test_publish_todo_shim_forwards_subject():
    p = OutlookPublisher()
    captured = {}

    def _spy(db_config, message_id, **kwargs):
        captured.update(kwargs)
        return True

    p.publish_mail = _spy  # type: ignore[assignment]
    p.publish_todo(
        object(), reference_id="m11", agent_view_id=1, subject="Re: Pytanie",
    )
    assert captured["subject"] == "Re: Pytanie"


# ---- S2: allow-list patterns escape every regex metachar (no fail-OPEN widening) ----

def test_question_mark_in_allowed_pattern_is_literal_not_quantifier():
    # '?' must be escaped, not treated as a regex 'optional' quantifier — otherwise pattern
    # "a?b@x.com" would also admit "b@x.com" (a WIDENING of the allow-list, the fail-OPEN direction).
    p = OutlookPublisher()
    patterns = ["a?b@x.com"]
    # the literal address still matches the literal pattern
    with patch("agento.modules.outlook.src.channel.publish", return_value=True):
        assert p.publish_mail(
            object(), "mq1", agent_view_id=1, sender_email="a?b@x.com",
            dmarc="pass", allowed_senders=patterns, logger=MagicMock(),
        ) is True
    # but the quantifier-widened address must NOT match
    with patch("agento.modules.outlook.src.channel.publish") as mock_publish:
        assert p.publish_mail(
            object(), "mq2", agent_view_id=1, sender_email="b@x.com",
            dmarc="pass", allowed_senders=patterns, logger=MagicMock(),
        ) is False
        mock_publish.assert_not_called()


# ---- Channel split: admit_mail / publish_admitted_mail / publish_mail wrapper parity ----

def test_admit_mail_returns_normalized_admission_on_pass():
    # admit_mail runs the gate and returns an OutlookAdmission with a strip+lower sender — NO job.
    p = OutlookPublisher()
    with patch("agento.modules.outlook.src.channel.publish") as mock_publish:
        admission = p.admit_mail(
            "m1", sender_email="  SKLEP@MyCompanyStudio.COM  ", dmarc="pass",
            allowed_senders=WHITELIST, logger=MagicMock(),
        )
    assert isinstance(admission, OutlookAdmission)
    assert admission.sender == "sklep@mycompanystudio.com"  # strip + lower
    mock_publish.assert_not_called()  # admission never publishes


def test_admit_mail_rejects_off_allowlist_and_dmarc_fail():
    p = OutlookPublisher()
    assert p.admit_mail("m1", sender_email="stranger@elsewhere.com", dmarc="pass",
                        allowed_senders=WHITELIST, logger=MagicMock()) is None
    assert p.admit_mail("m2", sender_email="sklep@mycompanystudio.com", dmarc="fail",
                        allowed_senders=WHITELIST, logger=MagicMock()) is None


def test_publish_admitted_mail_uses_admission_sender_for_key_and_email():
    # SUG3: the admitted sender IS the audited sender — used for BOTH the JobRequester.key sha256
    # digest AND the email field. No separate sender_email is accepted here.
    p = OutlookPublisher()
    admission = OutlookAdmission(sender="sklep@mycompanystudio.com")
    with patch("agento.modules.outlook.src.channel.publish", return_value=True) as mock_publish:
        ok = p.publish_admitted_mail(object(), "m1", admission, agent_view_id=3, priority=55,
                                     logger=MagicMock())
    assert ok is True
    requester = mock_publish.call_args.kwargs["requester"]
    expected_digest = hashlib.sha256(b"sklep@mycompanystudio.com").hexdigest()
    assert requester.email == "sklep@mycompanystudio.com"
    assert requester.key == f"outlook:email:{expected_digest}"
    assert requester.trust == RequesterTrust.DOMAIN
    assert mock_publish.call_args.kwargs["agent_view_id"] == 3
    assert mock_publish.call_args.kwargs["priority"] == 55


def test_publish_mail_wrapper_is_admit_then_publish_admitted():
    # publish_mail = admit_mail -> (None -> False) -> publish_admitted_mail. The requester it builds
    # is byte-identical to admitting + publishing the same message directly (the wrapper re-passes
    # the admission object, never a separately re-normalized sender_email).
    p = OutlookPublisher()
    raw = "  SKLEP@MyCompanyStudio.COM  "
    normalized = "sklep@mycompanystudio.com"

    with patch("agento.modules.outlook.src.channel.publish", return_value=True) as m_wrap:
        assert p.publish_mail(object(), "m1", agent_view_id=2, priority=50, sender_email=raw,
                              dmarc="pass", allowed_senders=WHITELIST, logger=MagicMock()) is True
    wrap_req = m_wrap.call_args.kwargs["requester"]

    admission = p.admit_mail("m1", sender_email=raw, dmarc="pass",
                             allowed_senders=WHITELIST, logger=MagicMock())
    with patch("agento.modules.outlook.src.channel.publish", return_value=True) as m_direct:
        p.publish_admitted_mail(object(), "m1", admission, agent_view_id=2, priority=50, logger=MagicMock())
    direct_req = m_direct.call_args.kwargs["requester"]

    assert wrap_req.email == direct_req.email == normalized
    assert wrap_req.key == direct_req.key == f"outlook:email:{hashlib.sha256(normalized.encode()).hexdigest()}"
    assert wrap_req.trust == direct_req.trust
    assert wrap_req.meta == direct_req.meta


def test_publish_mail_returns_false_when_admission_rejected():
    p = OutlookPublisher()
    with patch("agento.modules.outlook.src.channel.publish") as mock_publish:
        result = p.publish_mail(object(), "m1", agent_view_id=1, sender_email="stranger@elsewhere.com",
                                dmarc="pass", allowed_senders=WHITELIST, logger=MagicMock())
    assert result is False
    mock_publish.assert_not_called()
