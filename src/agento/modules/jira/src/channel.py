from __future__ import annotations

import logging
from datetime import UTC, datetime

from agento.framework.agent_view_runtime import resolve_publish_priority
from agento.framework.channels.base import PromptFragments, WorkItem
from agento.framework.db import get_connection
from agento.framework.job_models import AgentType, JobRequester, RequesterTrust
from agento.framework.publisher import publish
from agento.framework.router import RoutingContext, resolve_agent_view
from agento.modules.jira.src.mention_detector import find_unanswered_mention
from agento.modules.jira.src.task_list import TaskListBuilder
from agento.modules.jira.src.toolbox_client import ToolboxClient

_COMMENT_FORMAT = (
    "Komentarz pisz w Jira wiki markup, nie jako surowy blok tekstu: "
    "krótkie sekcje, czytelne nagłówki, płaskie listy, odstępy między akapitami. "
    "Formatowania używaj tam, gdzie poprawia skanowalność: *pogrubienie*, "
    "listy `*`, cytaty i dla poleceń, logów oraz snippetów. "
    "Unikaj nadmiarowego escape'owania znaków nowej linii i backslashy."
)


class JiraPromptChannel:
    """Channel concern: prompt fragments for agent instructions."""

    @property
    def name(self) -> str:
        return "jira"

    def get_prompt_fragments(self, reference_id: str, config: object | None = None) -> PromptFragments:
        return PromptFragments(
            read_context=(
                f"Użyj jira_get_issue aby pobrać szczegóły i komentarze {reference_id}.\n"
                "Zapamiętaj: Status, Reporter, ReporterAccountId, opis, komentarze."
            ),
            respond=(
                "Wynik dodaj jako komentarz (jira_add_comment), "
                "chyba że treść mówi inaczej (np. email_send). "
                + _COMMENT_FORMAT
            ),
            transition_start='Użyj jira_transition_issue z status_name "In Progress".',
            transition_done='Zmień status na "Review" (jira_transition_issue z status_name "Review").',
            assign_back='Przypisz na reportera (jira_assign_issue z assignee "reporter").',
            ask_and_handback=(
                "Jeśli masz pytania lub wątpliwości:\n"
                "  a) Napisz komentarz z pytaniami (jira_add_comment).\n"
                f"     {_COMMENT_FORMAT}\n"
                '  b) Przypisz zadanie na reportera (jira_assign_issue z assignee "reporter").\n'
                "  c) ZAKOŃCZ — nie wykonuj dalszych kroków.\n"
                "Jeśli wcześniej zadałeś pytania i nie ma odpowiedzi w komentarzach: ZAKOŃCZ."
            ),
        )

    def get_followup_fragments(
        self, reference_id: str, instructions: str, config: object | None = None
    ) -> PromptFragments:
        return PromptFragments(
            read_context=(
                f"Wczytaj zadanie i komentarze (jira_get_issue) — "
                f"sprawdź obecny stan i kontekst. Issue key: {reference_id}."
            ),
            respond=(
                "Wynik zwróć w komentarzu (jira_add_comment), "
                "chyba że instrukcje mówią inaczej (np. email_send). "
                + _COMMENT_FORMAT
            ),
            transition_done='Zmień status na "Review" (jira_transition_issue z status_name "Review").',
            assign_back='Przypisz na reportera (jira_assign_issue z assignee "reporter").',
            extra=(
                "KONTEKST — instrukcje z momentu planowania:\n"
                "---\n"
                f"{instructions}\n"
                "---"
            ),
        )


class JiraDiscovery:
    """DiscoverableChannel concern: find pending tasks assigned to the agent."""

    def discover_work(
        self, config: object, logger: logging.Logger
    ) -> list[WorkItem]:
        ai_user = config.jira_assignee or config.user
        if not ai_user:
            logger.warning("jira_assignee/user not set, cannot discover Jira work")
            return []

        toolbox = ToolboxClient(config.toolbox_url)
        builder = TaskListBuilder(toolbox, config, ai_user, logger)
        tasks = builder.get_todo_tasks()
        return [
            WorkItem(
                reference_id=t.issue.key,
                title=t.issue.summary,
                priority=t.priority.value,
                reason=t.reason,
                source_tag=t.source.value,
                updated=t.issue.updated,
            )
            for t in tasks
        ]


class JiraPublisher:
    """Publisher concern: job queue insertion + mention detection."""

    @property
    def name(self) -> str:
        return "jira"

    def _resolve_routing(
        self,
        db_config: object,
        workflow_type: str,
        logger: logging.Logger | None = None,
        payload: dict | None = None,
    ) -> tuple[int | None, int]:
        """Resolve agent_view_id and priority via ingress routing.

        Returns (agent_view_id, priority). Falls back to (None, 50) if no route found.
        """
        try:
            conn = get_connection(db_config)
        except Exception:
            return None, 50
        try:
            ctx = RoutingContext(
                channel="jira",
                workflow_type=workflow_type,
                identity_type="jira",
                identity_value="jira",
                payload=payload or {},
            )
            decision = resolve_agent_view(conn, ctx)
            if decision is None:
                return None, 50
            priority = resolve_publish_priority(conn, decision.agent_view_id)
            return decision.agent_view_id, priority
        except Exception:
            if logger:
                logger.warning("Routing failed, publishing without agent_view", exc_info=True)
            return None, 50
        finally:
            conn.close()

    def build_idempotency_key(
        self,
        agent_type: AgentType,
        reference_id: str | None,
        updated: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        if agent_type == AgentType.CRON and reference_id:
            # CRON jobs still use a minute bucket — these are fire-and-forget scheduled runs.
            return f"jira:cron:{reference_id}:{now.strftime('%Y%m%d_%H%M')}"
        elif agent_type == AgentType.TODO and reference_id:
            # TODO jobs dedup purely by the issue's `updated` timestamp — one job per distinct Jira change.
            # When `updated` is missing (e.g. manual re-publish by key), fall back to a per-minute bucket
            # so the same CLI invocation twice-in-a-row doesn't dedup against a long-lived earlier job.
            if updated:
                upd = updated[:16].replace("-", "").replace("T", "_").replace(":", "")
                return f"jira:todo:{reference_id}:u{upd}"
            return f"jira:todo:{reference_id}:{now.strftime('%Y%m%d_%H%M')}"
        else:
            # Dispatch key — minute bucket for consistency with the CRON path.
            return f"jira:todo:dispatch:{now.strftime('%Y%m%d_%H%M')}"

    def publish_cron(
        self,
        config: object,
        reference_id: str,
        logger: logging.Logger | None = None,
        agent_view_id: int | None = None,
        priority: int = 50,
        payload: dict | None = None,
    ) -> bool:
        if agent_view_id is None:
            agent_view_id, priority = self._resolve_routing(config, "cron", logger, payload=payload)
        idem_key = self.build_idempotency_key(AgentType.CRON, reference_id)
        return publish(
            config, AgentType.CRON, self.name, idem_key,
            reference_id=reference_id, logger=logger,
            agent_view_id=agent_view_id, priority=priority,
        )

    def publish_todo(
        self,
        config: object,
        reference_id: str | None = None,
        updated: str | None = None,
        logger: logging.Logger | None = None,
        agent_view_id: int | None = None,
        priority: int = 50,
        payload: dict | None = None,
        requester: JobRequester | None = None,
    ) -> bool:
        if agent_view_id is None:
            agent_view_id, priority = self._resolve_routing(config, "todo", logger, payload=payload)
        idem_key = self.build_idempotency_key(AgentType.TODO, reference_id, updated=updated)
        return publish(
            config, AgentType.TODO, self.name, idem_key,
            reference_id=reference_id, logger=logger,
            agent_view_id=agent_view_id, priority=priority,
            skip_if_active=True,
            requester=requester,
        )

    def publish_mentions(
        self,
        config: object,
        logger: logging.Logger,
        db_config: object | None = None,
        agent_view_id: int | None = None,
        priority: int = 50,
    ) -> int:
        """Find and publish unanswered mention jobs. Returns count of published jobs."""
        if agent_view_id is None:
            publish_cfg = db_config or config
            agent_view_id, priority = self._resolve_routing(publish_cfg, "todo", logger)
        agent_account_id = config.jira_assignee_account_id
        if not agent_account_id:
            logger.warning("jira_assignee_account_id not set, cannot detect mentions")
            return 0

        ai_user = config.jira_assignee or config.user
        if not ai_user:
            logger.warning("jira_assignee/user not set")
            return 0

        toolbox = ToolboxClient(config.toolbox_url)
        builder = TaskListBuilder(toolbox, config, ai_user, logger, agent_view_id=agent_view_id)
        candidates = builder.get_unanswered_mentions()

        publish_cfg = db_config or config
        published = 0
        for task in candidates:
            try:
                comments = toolbox.jira_get_comments(task.issue.key, agent_view_id=agent_view_id)
                mention = find_unanswered_mention(comments, agent_account_id)
                if mention is None:
                    logger.debug(f"{task.issue.key}: no unanswered mention, skipping")
                    continue

                comment_id = mention["id"]
                idem_key = f"jira:mention:{task.issue.key}:{comment_id}"
                author = mention.get("author") or {}
                account_id = author.get("accountId")
                requester = None
                if account_id:
                    requester = JobRequester(
                        key=f"jira:{account_id}",
                        email=author.get("emailAddress"),  # JobRequester normalizes (strip+lower)
                        trust=RequesterTrust.ACCOUNT,
                        meta={"basis": "comment_author", "issue_key": task.issue.key,
                              "comment_id": comment_id, "display_name": author.get("displayName")},
                    )
                inserted = publish(
                    publish_cfg, AgentType.TODO, self.name, idem_key,
                    reference_id=task.issue.key, logger=logger,
                    agent_view_id=agent_view_id, priority=priority,
                    requester=requester,
                )
                if inserted:
                    published += 1
            except Exception:
                logger.exception(f"Error processing mention candidate {task.issue.key}")

        return published


class JiraChannel(JiraPromptChannel, JiraDiscovery, JiraPublisher):
    """Backward-compatible facade — combines all three Jira concerns.

    New code should use the focused classes directly.
    """

    pass


# Module-level convenience functions (replace jira_publisher.py wrapper)
_jira = JiraPublisher()


def build_idempotency_key(
    agent_type: AgentType,
    issue_key: str | None,
    updated: str | None = None,
) -> str:
    return _jira.build_idempotency_key(agent_type, issue_key, updated=updated)


def publish_cron(
    config: object,
    issue_key: str,
    logger: logging.Logger | None = None,
    payload: dict | None = None,
) -> bool:
    return _jira.publish_cron(config, issue_key, logger, payload=payload)


def publish_todo(
    config: object,
    issue_key: str | None = None,
    updated: str | None = None,
    logger: logging.Logger | None = None,
    payload: dict | None = None,
    requester: JobRequester | None = None,
) -> bool:
    return _jira.publish_todo(
        config, issue_key, updated=updated, logger=logger,
        payload=payload, requester=requester,
    )


def publish_mentions(
    config: object,
    logger: logging.Logger | None = None,
    db_config: object | None = None,
) -> int:
    return _jira.publish_mentions(config, logger or logging.getLogger(__name__), db_config=db_config)
