"""Shared per-view publisher loop for both Bitbucket lanes (DRY across comments/changes)."""
from __future__ import annotations

import argparse
import logging
from contextlib import closing

from agento.framework.agent_view_runtime import resolve_publish_priority
from agento.framework.config_resolver import ScopedConfigService
from agento.framework.scoped_config import Scope, load_scoped_db_overrides
from agento.framework.toolbox_capability import (
    rest_capability,
)
from agento.framework.workspace import get_active_agent_views

from ..channel import BitbucketPublisher
from ..config import BitbucketConfig
from ..toolbox_client import BitbucketToolboxClient


def _view_scoped_bitbucket_identity(conn, agent_view_id: int) -> bool:
    """True iff this view has its OWN ``account_uuid`` AND ``repo_allowlist`` set at agent_view scope.

    A direct ``core_config_data`` check (NOT the resolved/inherited value): in a multi-view deployment a
    view that only inherits these from DEFAULT would resolve the SAME account/repos as every other view
    and fan the same PR out across views (broken attribution). ``run_lane`` skips such views. (R4-1/D-11)
    """
    rows = load_scoped_db_overrides(conn, Scope.AGENT_VIEW, agent_view_id)

    def _set(path: str) -> bool:
        entry = rows.get(path)
        return bool(entry and entry[0])

    return _set("bitbucket/bitbucket_account_uuid") and _set("bitbucket/repo_allowlist")


def run_lane(
    db_config: object,
    conn,
    toolbox_url: str,
    logger: logging.Logger,
    *,
    lane: str,
    agent_view_code: str | None = None,
    top_override: int | None = None,
) -> int:
    """Iterate active agent_views, ask the toolbox for each view's OPEN PRs, publish one job per PR with
    work. Per-view, per-repo and per-PR failures are isolated (logged, never abort the run). Returns the
    number of jobs published.
    """
    publisher = BitbucketPublisher()
    published = 0
    views = get_active_agent_views(conn)
    multi_view = len(views) > 1
    for av in views:
        if agent_view_code and av.code != agent_view_code:
            continue
        try:
            # Resolve ONLY the non-secret fields the publisher needs. We deliberately do NOT call
            # get_module("bitbucket") here: that resolves every system.json field — including the
            # obscure bitbucket_api_token, which the framework decrypts during resolution — whereas
            # per-path .get() never touches the token path. The token stays toolbox-only; the
            # publisher's own code never reads or decrypts it (and for the recommended agent_view-
            # scoped config the token is never decrypted in the publisher process at all).
            sc = ScopedConfigService(conn, Scope.AGENT_VIEW, av.id)
            cfg = BitbucketConfig.from_dict({
                "enabled": sc.get("bitbucket/enabled"),
                "bitbucket_workspace": sc.get("bitbucket/bitbucket_workspace"),
                "bitbucket_account_uuid": sc.get("bitbucket/bitbucket_account_uuid"),
                "repo_allowlist": sc.get("bitbucket/repo_allowlist"),
                "poll_top": sc.get("bitbucket/poll_top"),
            })
            if not cfg.enabled:
                continue  # inert until enabled (the toolbox re-enforces this too)
            if not cfg.repo_list:
                continue  # empty allow-list ⇒ skip early, no scan, no error
            if multi_view and not _view_scoped_bitbucket_identity(conn, av.id):
                logger.info(
                    "bitbucket: multi-view deployment; agent_view %s has no view-scoped "
                    "account_uuid/repo_allowlist — skipping to avoid DEFAULT fan-out",
                    av.code,
                )
                continue

            # One capability per view, minted and revoked around this view's calls:
            # the toolbox derives the view from it, so a client shared across views
            # would be a client with the wrong scope for all but one of them.
            with rest_capability(agent_view_id=av.id, db_config=db_config) as capability_token, closing(
                BitbucketToolboxClient(toolbox_url, capability_token=capability_token)
            ) as client:
                resp = client.open_prs(av.id, lane=lane, top=top_override)
            for err in resp.get("errors", []):
                logger.warning("bitbucket repo error (view %s): %s", av.code, err)

            priority = resolve_publish_priority(conn, av.id)
            for pr in resp.get("pull_requests", []):
                try:
                    if publisher.publish_pr(
                        db_config, pr, lane=lane, agent_view_id=av.id,
                        priority=priority, account_uuid=cfg.account_uuid, logger=logger,
                    ):
                        published += 1
                except Exception:
                    logger.exception(
                        "bitbucket publish failed for PR %s (view %s) — continuing",
                        pr.get("id"), av.code,
                    )
        except Exception:
            logger.exception("bitbucket lane=%s failed for view %s — continuing", lane, av.code)
    return published


def configure_lane_parser(parser: argparse.ArgumentParser) -> None:
    """Shared CLI args for both lane commands."""
    parser.add_argument("--agent-view", type=str, default=None, help="Only publish for this agent_view code")
    parser.add_argument("--top", type=int, default=None, help="Narrowing override of poll_top (<=50)")


def execute_lane(lane: str, args: argparse.Namespace) -> int:
    """Bootstrap, resolve the toolbox URL, and run one lane sweep. Returns jobs published.

    Mirrors OutlookPublishCommand.execute boilerplate; the per-view work lives in run_lane (testable in
    isolation). Holds the connection open across run_lane (publish() opens its own connection).
    """
    from agento.framework.bootstrap import bootstrap, get_module_config
    from agento.framework.cli.runtime import _load_framework_config
    from agento.framework.db import get_connection
    from agento.framework.log import get_logger

    logger = get_logger("publisher", "/app/logs/publisher.log", stderr=False)
    db_config, _, _ = _load_framework_config()
    conn = get_connection(db_config)
    try:
        bootstrap(db_conn=conn)
        core_cfg = get_module_config("core")
        toolbox_url = core_cfg.get("toolbox/url", "") if isinstance(core_cfg, dict) else ""
        if not toolbox_url:
            logger.warning("bitbucket: core/toolbox/url not set, skipping lane=%s", lane)
            return 0
        count = run_lane(
            db_config, conn, toolbox_url, logger,
            lane=lane, agent_view_code=args.agent_view, top_override=args.top,
        )
        logger.info("Published %d bitbucket %s jobs", count, lane)
        return count
    finally:
        conn.close()
