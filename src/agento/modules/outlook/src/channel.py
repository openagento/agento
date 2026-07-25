from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass

from agento.framework.channels.base import PromptFragments
from agento.framework.event_manager import get_event_manager
from agento.framework.events import SecurityBreachEvent
from agento.framework.job_models import AgentType, JobRequester, RequesterTrust
from agento.framework.publisher import publish
from agento.modules.outlook.src import activation

_REFERENCE_SEP = "::"  # multi-char: base64/base64url message ids never contain two consecutive ':'
_SLUG_MAX = 60
_REFERENCE_ID_MAX = 512  # job.reference_id column width (see migration 027)


def _slugify(subject: str | None, max_len: int = _SLUG_MAX) -> str:
    """ASCII, log-safe, deterministic subject slug for the reference_id prefix.

    'ł'/'Ł' are special-cased (NFKD does not decompose them); the rest of the Polish
    diacritics fold via NFKD + ascii-ignore. Non-[a-z0-9] runs collapse to a single '-'.
    Falls back to 'mail' when the subject yields nothing usable.
    """
    s = (subject or "").replace("ł", "l").replace("Ł", "L")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "mail"


def _build_reference_id(
    message_id: str, subject: str | None, *, max_total: int = _REFERENCE_ID_MAX, sep: str = _REFERENCE_SEP
) -> str:
    """Compose reference_id = '{slug}{sep}{message_id}' for log/admin readability.

    The message_id is sacred — it is recovered downstream via rsplit(sep, 1)[-1] and fed to the
    Graph tools, so it must survive intact and live at the tail. Only the slug is truncated to fit
    ``max_total``; if there is no room (or no subject) the bare message_id is stored.
    """
    if not subject or not subject.strip():
        return message_id
    budget = max_total - len(message_id) - len(sep)
    if budget <= 0:
        return message_id
    slug = _slugify(subject)[:budget].rstrip("-")
    return f"{slug}{sep}{message_id}" if slug else message_id


def _message_id_from_reference(reference_id: str) -> str:
    """Recover the bare Graph message_id from a (possibly slug-prefixed) reference_id."""
    return reference_id.rsplit(_REFERENCE_SEP, 1)[-1]


def _matches_allowed(sender: str, allowed_senders: list[str] | None) -> bool:
    """Reproduce core's ``matchesWhitelist`` semantics (email.js).

    Each pattern is anchored (``^...$``), case-insensitive (caller passes a lowered sender),
    and ``*`` expands to ``[^@]*`` so ``*@mycompany.com`` matches any local part but never crosses
    the ``@``. An empty/None allow-list matches nothing (fail-closed).
    """
    if not allowed_senders:
        return False
    for pattern in allowed_senders:
        # Escape EVERY regex metachar in the literal segments (split on the glob ``*``) so a pattern
        # like ``a?b@x.com`` matches literally — never as a regex quantifier, which would WIDEN the
        # allow-list (the fail-OPEN direction). ``*`` expands to ``[^@]*`` (matches a local part but
        # never crosses the ``@``). Kept in lockstep with the JS ``matchesWhitelist`` (outlook.js).
        regex = "^" + "[^@]*".join(re.escape(seg) for seg in pattern.lower().split("*")) + "$"
        if re.match(regex, sender):
            return True
    return False


class OutlookPromptChannel:
    """Channel concern: Polish prompt fragments for email tasks."""

    @property
    def name(self) -> str:
        return "outlook"

    def get_prompt_fragments(self, reference_id: str) -> PromptFragments:
        message_id = _message_id_from_reference(reference_id)
        return PromptFragments(
            # Short fixed opening — the long compound reference_id (subject-slug::message-id) is not
            # repeated in the prompt; the bare message_id arrives via read_context below.
            task_intro="Wykonaj zadanie z wiadomości email.",
            read_context=(
                f"Użyj outlook_get_message aby pobrać treść emaila o message_id: {message_id}.\n"
                "Zapamiętaj: temat, nadawcę, treść, datę otrzymania."
            ),
            respond=(
                "Wynik odeślij odpowiadając na email (outlook_reply z message_id i treścią odpowiedzi). "
                "Treść odpowiedzi sformatuj jako poprawny HTML (akapity <p>, listy <ul>/<li>, "
                "pogrubienia <b>) — NIE zwykły tekst ani markdown."
            ),
            transition_done="Oznacz email jako przetworzony (outlook_mark_processed z message_id).",
            ask_and_handback=(
                "Jeśli masz pytania lub wątpliwości:\n"
                "  a) Odpowiedz na email z pytaniami (outlook_reply).\n"
                "  b) ZAKOŃCZ — nie wykonuj dalszych kroków.\n"
                "Jeśli wcześniej zadałeś pytania i nie ma odpowiedzi: ZAKOŃCZ."
            ),
        )

    def get_followup_fragments(self, reference_id: str, instructions: str) -> PromptFragments:
        message_id = _message_id_from_reference(reference_id)
        return PromptFragments(
            # Short fixed opening — the long reference_id is not repeated in the prompt; the bare
            # message_id arrives via read_context below.
            followup_intro="Kontynuuj zadanie z wiadomości email.",
            read_context=(
                f"Wczytaj email (outlook_get_message) — sprawdź obecny stan i kontekst. "
                f"Message ID: {message_id}."
            ),
            respond=(
                "Wynik zwróć odpowiadając na email (outlook_reply). "
                "Treść odpowiedzi sformatuj jako poprawny HTML (akapity <p>, listy <ul>/<li>, "
                "pogrubienia <b>) — NIE zwykły tekst ani markdown."
            ),
            transition_done="Oznacz email jako przetworzony (outlook_mark_processed).",
            extra=(
                "KONTEKST — instrukcje z momentu planowania:\n"
                "---\n"
                f"{instructions}\n"
                "---"
            ),
        )


@dataclass(frozen=True)
class OutlookAdmission:
    """A message that passed the inbound security gate. ``sender`` is normalized (strip+lower) and
    is the single audited identity used for BOTH routing (regex/priority) and the JobRequester."""

    sender: str


class OutlookPublisher:
    """Publisher concern: admit inbound mail through the security gate (``admit_mail``), then publish
    an admitted message to a chosen agent_view (``publish_admitted_mail``). ``publish_mail`` is the
    thin admit->publish wrapper used by direct (single-view) polling; routed (shared-mailbox) polling
    admits once, routes by sender, then calls ``publish_admitted_mail`` for the resolved target."""

    @property
    def name(self) -> str:
        return "outlook"

    def build_idempotency_key(self, message_id: str) -> str:
        return f"outlook:mail:{message_id}"

    def admit_mail(
        self, message_id: str, *, sender_email: str | None = None,
        dmarc: str | None = None, allowed_senders: list[str] | None = None,
        subject: str | None = None,
        to=None, cc=None, body_preview: str | None = None,
        agent_authored: bool = False, mailbox: str | None = None, aliases=None, cfg=None,
        logger: logging.Logger | None = None,
    ) -> OutlookAdmission | None:
        """Run the inbound security gate (allowed_senders -> DMARC -> activation) and return an
        ``OutlookAdmission`` carrying the normalized sender, or ``None`` on any reject. NO job is
        created here — publishing is a separate step (``publish_admitted_mail``)."""
        # 1. Normalize the claimed From address.
        sender = (sender_email or "").strip().lower()

        # 2. ALLOWED-SENDERS GATE (fail-closed). A sender that does not match is ordinary
        #    non-routing — NOT a breach. Log the domain only (the local part of an unauthorized
        #    external address is PII we never need), leave the email unread.
        if not _matches_allowed(sender, allowed_senders):
            if logger:
                sender_domain = sender.split("@")[-1] if "@" in sender else "?"
                logger.info(
                    "Outlook sender not in allowed_senders; leaving unread",
                    extra={"message_id": message_id[:40], "sender_domain": sender_domain},
                )
            return None

        # 3. DMARC GATE (unconditional — a hard DMARC pass is always required to publish). Two distinct
        #    non-pass cases, both fail-closed (never published):
        #      * EXPLICIT failure (fail/quarantine/reject) on a domain that publishes a DMARC policy is a
        #        probable SPOOF -> SECURITY_BREACH log (greppable marker + full claimed From, justified
        #        for a flagged spoof) AND an ops alert via the framework security_breach event.
        #      * Any other non-pass (none / bestguesspass / temperror / missing verdict) just means the
        #        sender's domain has no usable DMARC policy — NOT a spoof. Log info (domain only, no
        #        breach, no alert): EOP emits dmarc=bestguesspass for recordless domains on every poll,
        #        and flagging that as a breach would flood the log and dilute the marker.
        verdict = (dmarc or "").lower()
        if verdict != "pass":
            if verdict in ("fail", "quarantine", "reject"):
                if logger:
                    logger.error(
                        "SECURITY_BREACH: whitelisted outlook sender failed DMARC (probable spoof)",
                        extra={
                            "event": "security_breach",
                            "reason": "dmarc_not_pass",
                            "sender": sender,
                            "dmarc": dmarc,
                            "message_id": message_id[:40],
                        },
                    )
                self._alert_security_breach(message_id, sender, verdict, logger)
            elif logger:
                sender_domain = sender.split("@")[-1] if "@" in sender else "?"
                logger.info(
                    "Outlook sender has no confirmed DMARC pass; leaving unread (not a spoof)",
                    extra={"message_id": message_id[:40], "dmarc": verdict or "none",
                           "sender_domain": sender_domain},
                )
            return None

        # 4. ACTIVATION GATE (stateless). Speak only when clearly meant to: addressed to the mailbox
        #    (direct) or summoned by token (mention), and never in response to another agent's mail
        #    (agent_authored — sender is a fleet mailbox) unless collaboration is opted in. A silent
        #    decision is NOT a hold — the
        #    caller advances the cursor exactly as it does for other "not for us" mail. Skipped when
        #    no cfg is supplied (legacy/direct callers) so the plain security gate stands alone.
        if cfg is not None:
            decision = activation.decide(
                agent_authored=agent_authored, cfg=cfg, mailbox=mailbox, aliases=aliases,
                to_addrs=to, cc_addrs=cc, subject=subject, body_preview=body_preview,
            )
            if not decision.can_respond:
                if logger:
                    logger.info(
                        "Outlook message not activated; leaving unread",
                        extra={"message_id": message_id[:40], "reason": decision.reason},
                    )
                return None

        return OutlookAdmission(sender=sender)

    def publish_admitted_mail(
        self, db_config: object, message_id: str, admission: OutlookAdmission, *,
        agent_view_id: int, priority: int = 50, subject: str | None = None,
        logger: logging.Logger | None = None,
    ) -> bool:
        """Publish an already-admitted message to ``agent_view_id``. The sender is taken ONLY from
        ``admission.sender`` (already strip+lower) — the identity that passed the gate (and in routed
        mode chose the route) IS the audited requester, used for BOTH the JobRequester.key sha256
        digest AND the email field. DMARC pass cryptographically aligned the From domain -> trust=DOMAIN.
        """
        sender = admission.sender
        digest = hashlib.sha256(sender.encode()).hexdigest()
        requester = JobRequester(
            key=f"outlook:email:{digest}",  # 14 + 64 = 78 chars, always < 255
            email=sender,  # already normalized (strip+lower)
            trust=RequesterTrust.DOMAIN,
            meta={"basis": "email_from", "dmarc": "pass"},
        )
        # reference_id carries a human-readable subject slug for logs/admin; the bare message_id
        # lives at the tail (recovered via _message_id_from_reference for the agent's Graph tools).
        # The idempotency_key stays bare so dedup keys only on the immutable message_id.
        return publish(
            db_config, AgentType.TODO, self.name,
            self.build_idempotency_key(message_id),
            reference_id=_build_reference_id(message_id, subject), logger=logger,
            agent_view_id=agent_view_id, priority=priority,
            skip_if_active=True, requester=requester,
        )

    def publish_mail(
        self, db_config: object, message_id: str, *, agent_view_id: int,
        priority: int = 50, sender_email: str | None = None,
        dmarc: str | None = None, allowed_senders: list[str] | None = None,
        subject: str | None = None,
        to=None, cc=None, body_preview: str | None = None,
        agent_authored: bool = False, mailbox: str | None = None, aliases=None, cfg=None,
        logger: logging.Logger | None = None,
    ) -> bool:
        """Thin admit->publish wrapper (direct/single-view polling): admit the message through the
        security gate, then publish it to ``agent_view_id`` on pass (else return False)."""
        admission = self.admit_mail(
            message_id, sender_email=sender_email, dmarc=dmarc, allowed_senders=allowed_senders,
            subject=subject, to=to, cc=cc, body_preview=body_preview,
            agent_authored=agent_authored, mailbox=mailbox, aliases=aliases, cfg=cfg, logger=logger,
        )
        if admission is None:
            return False
        return self.publish_admitted_mail(
            db_config, message_id, admission, agent_view_id=agent_view_id,
            priority=priority, subject=subject, logger=logger,
        )

    def _alert_security_breach(
        self, message_id: str, sender: str, dmarc: str, logger: logging.Logger | None,
    ) -> None:
        """Notify ops of a probable spoof via the framework ``security_breach_after`` event.

        Decoupled from any alert transport: app_monitor (when enabled) observes the event and emails
        ops. ``EventManager.dispatch`` swallows observer errors, so a failing alert never blocks the
        poll loop; the surrounding try/except only guards the dispatch call itself.
        """
        try:
            get_event_manager().dispatch(
                "security_breach_after",
                SecurityBreachEvent(
                    channel="outlook",
                    reason="dmarc_not_pass",
                    sender=sender,
                    reference_id=message_id,
                    detail=f"dmarc={dmarc}",
                ),
            )
        except Exception:
            if logger:
                logger.warning("Failed to dispatch security_breach event", exc_info=True)

    # Publisher protocol shims (the framework Publisher protocol expects these names).
    def publish_todo(self, config: object, reference_id: str | None = None, **kwargs) -> bool:
        if not reference_id:
            raise ValueError("outlook publish_todo requires a reference_id (message_id)")
        return self.publish_mail(
            config, reference_id,
            agent_view_id=kwargs["agent_view_id"],
            priority=kwargs.get("priority", 50),
            sender_email=kwargs.get("sender_email"),
            dmarc=kwargs.get("dmarc"),
            allowed_senders=kwargs.get("allowed_senders"),
            subject=kwargs.get("subject"),
            logger=kwargs.get("logger"),
        )

    def publish_cron(self, config: object, reference_id: str, **kwargs) -> bool:
        raise NotImplementedError("outlook channel does not support cron publishing")


class OutlookChannel(OutlookPromptChannel, OutlookPublisher):
    """Facade combining the prompt + publisher concerns for the outlook channel."""

    pass
