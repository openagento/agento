"""CLI command: outlook:publish — poll each shared/solo mailbox group (Graph delta cursor) for new email and publish jobs."""
from __future__ import annotations

import argparse
import logging

from agento.framework.agent_view_runtime import resolve_publish_priority
from agento.framework.config_resolver import ScopedConfigService
from agento.framework.event_manager import get_event_manager
from agento.framework.events import InboundRouteDropEvent, MailboxStalledEvent
from agento.framework.ingress_identity import get_active_identities_for_type
from agento.framework.log import get_logger
from agento.framework.router import RoutingContext, resolve_agent_view
from agento.framework.scoped_config import Scope
from agento.framework.workspace import get_active_agent_views
from agento.modules.outlook.src.channel import OutlookPublisher
from agento.modules.outlook.src.config import OutlookConfig
from agento.modules.outlook.src.cursor import load_cursors, save_cursor
from agento.modules.outlook.src.toolbox_client import OutlookToolboxClient

# Non-secret outlook config paths, read per-path (never get_module). NOTE: this per-path discipline
# keeps the ROUTING code below from resolving Graph secrets, but the surrounding execute() still
# calls bootstrap(db_conn=conn), which transiently decrypts DEFAULT-scope obscure config (incl. the
# Graph creds) in the cron — a pre-existing, framework-wide limitation tracked by the toolbox-only
# secret-boundary hardening PRD (docs/security/toolbox-only-secret-boundary.md), NOT closed here.
_CONFIG_PATHS = (
    "enabled", "poll_top", "allowed_senders", "activation_modes", "summon_token",
    "direct_requires_sole_recipient", "mailbox_aliases", "allow_bot_collaboration",
)

# The mailbox UPN is a NON-secret string; the Python publisher resolves it (scoped fallback) to
# group views BEFORE polling. It is deliberately NOT an OutlookConfig field (toolbox-adjacent).
_MAILBOX_PATH = "outlook/outlook_mailbox_user_id"

# The MAILBOX-LEVEL activation policy inputs that MUST be identical across members of a shared
# (routed) mailbox: activation runs ONCE with the poll-owner's cfg, so a divergence would let the
# lowest-id member silently define activation for the whole mailbox. allowed_senders is deliberately
# EXCLUDED — it is now a PER-VIEW gate (union pre-filter pre-route + per-view refinement post-route),
# so members may legitimately differ on it. restrict_read_to_allowed_senders is likewise excluded
# (a toolbox read-tool gate resolved per TARGET view at read time, not an admit_mail input).
_MAILBOX_POLICY_FIELDS = (
    "activation_modes", "summon_token",
    "direct_requires_sole_recipient", "mailbox_aliases", "allow_bot_collaboration",
)


def _resolve_outlook_config(conn, agent_view_id: int) -> OutlookConfig:
    svc = ScopedConfigService(conn, Scope.AGENT_VIEW, agent_view_id)
    return OutlookConfig.from_dict({key: svc.get(f"outlook/{key}") for key in _CONFIG_PATHS})


def _resolve_mailbox_upn(conn, agent_view_id: int) -> str:
    svc = ScopedConfigService(conn, Scope.AGENT_VIEW, agent_view_id)
    return (svc.get(_MAILBOX_PATH) or "").strip().lower()


def _shared_policy_divergence(group_views, view_cfgs) -> list[tuple[str, str]]:
    """Return ``[(view_code, field)]`` where a member's mailbox-level activation policy field differs
    from the poll owner's. ``allowed_senders`` is NOT checked here (it is per-view). Empty list =
    consistent activation policy across the whole group."""
    owner = min(group_views, key=lambda v: v.id)
    base = view_cfgs[owner.id]
    divergent: list[tuple[str, str]] = []
    for v in group_views:
        if v.id == owner.id:
            continue
        cfg = view_cfgs[v.id]
        for field in _MAILBOX_POLICY_FIELDS:
            if getattr(cfg, field) != getattr(base, field):
                divergent.append((v.code, field))
    return divergent


def _publish_view_messages(publisher, db_config, av, cfg, messages, priority, logger, mailbox, aliases):
    """Gate+publish each message. Returns (published_count, hold). hold=True means a genuinely
    TRANSIENT condition (a publish exception, e.g. a DB blip) was seen → caller must NOT advance the
    cursor so the batch is re-fetched next poll. Per-message errors never abort the batch.

    A non-pass DMARC verdict — including ``temperror`` — must NOT hold. The verdict is read from the
    immutable receipt-time Authentication-Results header (parseDmarcVerdict), so it never changes on
    re-fetch; holding on it would pin the cursor forever (re-fetch grows without bound — the exact
    bounded-load regression DECISIONS.md resolves against). Such mail simply advances unpublished
    (re-evaluable only via a deliberate cursor resync)."""
    published = 0
    hold = False
    for msg in messages:
        message_id = msg.get("id")
        if not message_id:
            continue
        sender = (msg.get("from") or {}).get("address")
        try:
            if publisher.publish_mail(
                db_config, message_id, agent_view_id=av.id, priority=priority,
                sender_email=sender, dmarc=msg.get("dmarc"),
                allowed_senders=cfg.allowed_senders_list,
                subject=msg.get("subject"),
                to=msg.get("to"), cc=msg.get("cc"),
                body_preview=msg.get("bodyPreview"),
                agent_authored=bool(msg.get("agent_authored")),
                mailbox=mailbox, aliases=aliases, cfg=cfg,
                logger=logger,
            ):
                published += 1
        except Exception:
            logger.exception(f"Error publishing outlook message {message_id[:20]}... (view {av.code})")
            hold = True  # transient publish failure (e.g. DB blip) — do not advance past it
    return published, hold


def _publish_group_routed(publisher, db_config, conn, group_views, cfg, view_cfgs, messages, logger, mailbox, aliases):
    """Routed (shared-mailbox) mode: admit each message against the group's UNION allow-list (+ DMARC
    + mailbox-level activation, all pre-route), route by NORMALIZED sender to a member agent_view,
    refine against the routed-to view's own allow-list, and publish to the resolved target. Returns
    (published, hold).

    Cursor discipline: deterministic verdicts (rejected by the gate, no route, ambiguous tie, or a
    target outside this group) ADVANCE — they will not change on re-fetch. Only genuine transients
    hold: a router/DB exception (resolve_agent_view raises with fail_on_router_error=True) or a
    publish exception. Per-sender memoization caches ONLY the deterministic decision + job priority
    (never a transient hold), so resolve_agent_view and resolve_publish_priority each run at most
    once per unique sender per poll — work scales with DISTINCT senders, not message count (a
    backlog/resync re-emits the same senders many times over)."""
    group_ids = {v.id for v in group_views}
    # Auto-derived UNION of every member's per-view allow-list — the cheap in-memory pre-filter that
    # rejects senders no persona trusts BEFORE any routing DB/regex work (and scopes the DMARC breach
    # alert to union-trusted senders). Recomputed per poll; no manual union upkeep, no DB.
    union_allowed = sorted({p for v in group_views for p in view_cfgs[v.id].allowed_senders_list})
    # Post-admission (post-DMARC/-activation) drops, per unique sender per poll — surfaced once per
    # poll as an InboundRouteDropEvent + summary log (below).
    dropped = {"unroutable": 0, "ambiguous": 0, "per_view_allowlist": 0}
    # sender -> (target_view_id, priority) for a publishable route, or None for a deterministic drop.
    memo: dict[str, tuple[int, int] | None] = {}
    published = 0
    hold = False
    for msg in messages:
        message_id = msg.get("id")
        if not message_id:
            continue
        admission = publisher.admit_mail(
            message_id,
            sender_email=(msg.get("from") or {}).get("address"),
            dmarc=msg.get("dmarc"),
            allowed_senders=union_allowed,
            subject=msg.get("subject"),
            to=msg.get("to"), cc=msg.get("cc"),
            body_preview=msg.get("bodyPreview"),
            agent_authored=bool(msg.get("agent_authored")),
            mailbox=mailbox, aliases=aliases, cfg=cfg, logger=logger,
        )
        if admission is None:
            continue  # rejected by the gate (union allow-list / DMARC / activation) — advance
        sender = admission.sender
        if sender not in memo:
            try:
                ctx = RoutingContext(
                    channel="outlook", workflow_type="todo",
                    identity_type="outlook_sender", identity_value=sender,
                    payload={"mailbox": mailbox, "message_id": message_id},
                )
                decision = resolve_agent_view(conn, ctx, fail_on_router_error=True)
            except Exception:
                # Transient router/DB error — hold (re-fetched next poll), NEVER memoized.
                logger.exception("Outlook routing failed for a message in mailbox %s — holding", mailbox)
                hold = True
                continue
            if decision is None or decision.ambiguous or decision.agent_view_id not in group_ids:
                memo[sender] = None
                reason = "ambiguous" if (decision is not None and decision.ambiguous) else "unroutable"
                dropped[reason] += 1
                logger.info(
                    "Outlook routed-mode message not deliverable (no match / ambiguous / target "
                    "outside group); advancing",
                    extra={"message_id": message_id[:40], "mailbox": mailbox, "drop_reason": reason},
                )
            elif not publisher.sender_allowed(sender, view_cfgs[decision.agent_view_id].allowed_senders_list):
                # Post-route per-view refinement (fail-closed): the sender passed the UNION but is not
                # in the ROUTED-TO view's own allow-list -> deterministic drop (advance, no job).
                memo[sender] = None
                dropped["per_view_allowlist"] += 1
                sender_domain = sender.split("@")[-1] if "@" in sender else "?"
                logger.info(
                    "Outlook routed message rejected by routed-to view's allowed_senders; advancing",
                    extra={"message_id": message_id[:40], "mailbox": mailbox,
                           "agent_view_id": decision.agent_view_id, "sender_domain": sender_domain},
                )
            else:
                memo[sender] = (
                    decision.agent_view_id,
                    resolve_publish_priority(conn, decision.agent_view_id),
                )
        routed = memo[sender]
        if routed is None:
            continue  # deterministic drop — advance
        target_view_id, priority = routed
        try:
            if publisher.publish_admitted_mail(
                db_config, message_id, admission,
                agent_view_id=target_view_id, priority=priority,
                subject=msg.get("subject"), logger=logger,
            ):
                published += 1
        except Exception:
            logger.exception(
                f"Error publishing outlook message {message_id[:20]}... (routed → view {target_view_id})"
            )
            hold = True
    if any(dropped.values()):
        logger.info(
            "Outlook routed-poll drop summary",
            extra={"mailbox": mailbox, "unroutable": dropped["unroutable"],
                   "ambiguous": dropped["ambiguous"],
                   "per_view_allowlist": dropped["per_view_allowlist"]},
        )
        get_event_manager().dispatch(
            "inbound_route_drop_after",
            InboundRouteDropEvent(
                channel="outlook", mailbox=mailbox,
                unroutable=dropped["unroutable"], ambiguous=dropped["ambiguous"],
                per_view_allowlist=dropped["per_view_allowlist"],
            ),
        )
    return published, hold


def publish_all_views(
    db_config, conn, toolbox_url: str, logger: logging.Logger,
    *, agent_view_code: str | None = None, top_override: int | None = None,
) -> int:
    """Group active outlook-enabled agent_views by their resolved mailbox UPN, poll each group once
    via the Graph delta cursor, and publish new mail.

    A UPN owned by exactly ONE view is DIRECT mode (no routing, no binding required — admission /
    routing / publishing semantics unchanged). A UPN shared by >=2 views is ROUTED mode: the group is
    polled once by the lowest-id member; each message is admitted against the group's UNION
    allow-list (+ DMARC + mailbox-level activation), routed to a member view by matching the
    normalized sender against ``outlook_sender`` ingress bindings (regex, priority), then refined
    against the routed-to view's own ``allowed_senders``. Persist-then-advance: the cursor
    is written only AFTER a clean pass (never on a transient hold). The mailbox is never mutated.
    No active/eligible views -> clean no-op. Per-group errors log + continue. The toolbox client is
    always closed."""
    views = get_active_agent_views(conn)
    client = OutlookToolboxClient(toolbox_url)
    publisher = OutlookPublisher()
    cursors = load_cursors(conn)
    published = 0
    try:
        # Build mailbox groups BEFORE polling: {normalized UPN: [views]} over outlook-enabled views
        # with a configured mailbox.
        groups: dict[str, list] = {}
        view_cfgs: dict[int, OutlookConfig] = {}
        for av in views:
            cfg = _resolve_outlook_config(conn, av.id)
            view_cfgs[av.id] = cfg
            if not cfg.enabled:
                logger.debug("Outlook disabled for agent_view %s (id=%d), skipping", av.code, av.id)
                continue
            upn = _resolve_mailbox_upn(conn, av.id)
            if not upn:
                logger.warning("Outlook mailbox unconfigured for agent_view %s (id=%d), skipping", av.code, av.id)
                continue
            groups.setdefault(upn, []).append(av)

        # --agent-view: process only the group CONTAINING that view, WITHOUT shrinking it (a filtered
        # shared mailbox stays routed — PRD §7.4).
        if agent_view_code:
            groups = {
                upn: gv for upn, gv in groups.items()
                if any(v.code == agent_view_code for v in gv)
            }

        # Publisher-start effective-policy log: one line per enabled view (code, mailbox, mode, and
        # the effective allowed_senders COUNT — never the raw patterns, which may be external
        # addresses/domains; use `config:resolve` for the resolved values). Standalone loop so a
        # per-group error below never skips a policy log.
        for upn, group_views in groups.items():
            mode = "routed" if len(group_views) >= 2 else "direct"
            for av in group_views:
                vc = view_cfgs[av.id]
                logger.info(
                    "Effective outlook policy",
                    extra={"agent_view": av.code, "mailbox": upn, "mode": mode,
                           "allowed_senders_count": len(vc.allowed_senders_list)},
                )

        zero_bindings: bool | None = None  # batch-independent, computed once when first needed
        for upn, group_views in groups.items():
            try:
                poll_owner = min(group_views, key=lambda v: v.id)
                cfg = view_cfgs[poll_owner.id]
                routed = len(group_views) >= 2
                if routed:
                    divergent = _shared_policy_divergence(group_views, view_cfgs)
                    if divergent:
                        where = ", ".join(f"{code}:{field}" for code, field in divergent)
                        logger.error(
                            "Outlook shared mailbox %s: members have divergent activation policy "
                            "config (%s); skipping group (not polled) until reconciled", upn, where,
                        )
                        get_event_manager().dispatch(
                            "mailbox_stall_after",
                            MailboxStalledEvent(
                                channel="outlook", mailbox=upn,
                                reason="policy_divergence", detail=where,
                            ),
                        )
                        continue
                top = top_override if top_override else cfg.poll_top
                resp = client.list_delta(top=top, agent_view_id=poll_owner.id, cursors=cursors)
                mailbox_key = (resp.get("mailbox") or "").strip().lower()
                if not mailbox_key:
                    logger.warning(
                        "Outlook mailbox unresolved for agent_view %s (id=%d), skipping",
                        poll_owner.code, poll_owner.id,
                    )
                    continue
                if mailbox_key != upn:
                    # Config UPN and the toolbox-resolved mailbox disagree — a resolution drift.
                    # Hold (do not advance) rather than publish against the wrong cursor.
                    logger.warning(
                        "Outlook mailbox mismatch for agent_view %s: config=%s resolved=%s; holding",
                        poll_owner.code, upn, mailbox_key,
                    )
                    get_event_manager().dispatch(
                        "mailbox_stall_after",
                        MailboxStalledEvent(
                            channel="outlook", mailbox=upn, reason="upn_mismatch",
                            detail=f"configured UPN resolved to {mailbox_key}",
                        ),
                    )
                    continue
                messages = resp.get("messages", [])
                if routed:
                    if zero_bindings is None:
                        zero_bindings = not get_active_identities_for_type(conn, "outlook_sender")
                    if zero_bindings:
                        logger.warning(
                            "Outlook shared mailbox %s is in routed mode but no active "
                            "outlook_sender bindings exist — mail will NOT be published. Configure "
                            "`agento ingress:bind outlook_sender '<regex>' <view> --priority <n>`.",
                            upn,
                        )
                        get_event_manager().dispatch(
                            "mailbox_stall_after",
                            MailboxStalledEvent(
                                channel="outlook", mailbox=upn, reason="no_bindings",
                                detail="routed mode but no active outlook_sender bindings",
                            ),
                        )
                    pub_count, hold = _publish_group_routed(
                        publisher, db_config, conn, group_views, cfg, view_cfgs, messages, logger,
                        mailbox_key, cfg.mailbox_aliases_list,
                    )
                else:
                    priority = resolve_publish_priority(conn, poll_owner.id)
                    pub_count, hold = _publish_view_messages(
                        publisher, db_config, poll_owner, cfg, messages, priority, logger,
                        mailbox_key, cfg.mailbox_aliases_list,
                    )
                published += pub_count
                # PERSIST-THEN-ADVANCE: only after publishing, and only when the batch had no
                # transient condition. A held / errored cursor is re-fetched on the next poll.
                new_link = resp.get("deltaLink")
                if new_link and not hold:
                    save_cursor(conn, mailbox_key, new_link)
            except Exception:
                logger.exception(
                    "Outlook publish failed for mailbox group %s — continuing with remaining groups", upn,
                )
                continue
    finally:
        client.close()
    return published


class OutlookPublishCommand:
    @property
    def name(self) -> str:
        return "outlook:publish"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Poll each Outlook mailbox group (Graph delta cursor) and publish new email as jobs"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--top", type=int, default=None,
                            help="Delta page size per group (<=50); overrides poll_top (the poll still pages to the end)")
        parser.add_argument("--agent-view", dest="agent_view", default=None,
                            help="Run only the group containing this agent_view code (manual/debug); a shared mailbox stays routed")

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.bootstrap import bootstrap, get_module_config
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.db import get_connection

        logger = get_logger("publisher", "/app/logs/publisher.log", stderr=False)
        db_config, _, _ = _load_framework_config()
        conn = get_connection(db_config)
        try:
            bootstrap(db_conn=conn)
            outlook_cfg = get_module_config("outlook")
            toolbox_url = getattr(outlook_cfg, "toolbox_url", "") if outlook_cfg else ""
            if not toolbox_url:
                logger.error("core/toolbox/url not set; cannot poll Outlook")
                return
            count = publish_all_views(
                db_config, conn, toolbox_url, logger,
                agent_view_code=args.agent_view, top_override=args.top,
            )
            logger.info(f"Published {count} outlook-mail jobs")
        finally:
            conn.close()
