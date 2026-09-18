from __future__ import annotations

import logging

from agento.framework.channels.base import PromptFragments
from agento.framework.job_models import AgentType, JobRequester, RequesterTrust
from agento.framework.publisher import publish

from .review_scan import (
    build_changes_key,
    build_comments_key,
    detect_changes_requested,
    flag_unanswered,
)

# Lane tokens (the toolbox `lane` arg + publish_pr selector).
LANE_COMMENTS = "comments"
LANE_CHANGES = "changes"

# Published job.source per lane. These MUST equal the registered channel `.name` values below, because
# the framework resolves a job's prompt channel via get_channel(job.source) keyed on the instance .name
# (registry.py / consumer.py) — and distinct sources are what make skip_if_active dedup the two lanes
# independently (a running sweep job for a PR must not block the urgent changes-requested job). (D-7)
SOURCE_COMMENTS = "bitbucket-comments"
SOURCE_CHANGES = "bitbucket-changes"

# Fast lane (changes-requested) is prioritized above the view's base priority; capped at 100 because the
# consumer claims by `priority DESC` (higher = sooner).
CHANGES_PRIORITY_BUMP = 30


class BitbucketPromptChannel:
    """Channel concern: Polish prompt fragments for a PR-review task.

    Subclassed per lane (the two registered channel instances) — they differ only in the one-line intro;
    the read/respond/handback discipline is identical. The agent must FIRST read the PR and confirm it is
    still OPEN before doing anything (F10 / ACC "a PR that closes mid-work finishes cleanly"); the toolbox
    write tools enforce the same gate as defence-in-depth.
    """

    _source: str = ""
    _intro: str = ""

    @property
    def name(self) -> str:
        return self._source

    def get_prompt_fragments(self, reference_id: str, config: object | None = None) -> PromptFragments:
        return PromptFragments(
            read_context=(
                f"Zadanie code-review dla pull requesta {reference_id} ({self._intro}).\n"
                f"NAJPIERW pobierz PR (bitbucket_get_pr) i sprawdź, czy nadal jest OTWARTY (state == OPEN). "
                "Jeśli jest zmergowany/odrzucony/zamknięty — ZAKOŃCZ czysto, bez komentarzy i bez push.\n"
                "Następnie wczytaj diff (bitbucket_get_pr_diff), komentarze (bitbucket_get_pr_comments) "
                "i historię recenzji (bitbucket_get_pr_activity)."
            ),
            respond=(
                "Odnieś się do feedbacku recenzentów: odpowiedz na komentarze (bitbucket_add_comment, w razie "
                "potrzeby inline na konkretnej linii pliku). Jeśli wymagane są zmiany w kodzie, wypchnij commity "
                "na gałąź źródłową PR przy użyciu tożsamości git workspace'u."
            ),
            transition_done=(
                "Po rozwiązaniu wątku oznacz go jako resolved (bitbucket_resolve_comment); jeśli to właściwe, "
                "ustaw decyzję recenzji (bitbucket_set_review). Wykonuj wyłącznie te akcje, których narzędzia są "
                "włączone."
            ),
            ask_and_handback=(
                "Jeśli masz pytania lub wątpliwości:\n"
                "  a) Zadaj je w komentarzu do PR (bitbucket_add_comment).\n"
                "  b) ZAKOŃCZ — nie wykonuj dalszych kroków.\n"
                "Jeśli wcześniej zadałeś pytania i nie ma odpowiedzi: ZAKOŃCZ."
            ),
        )

    def get_followup_fragments(
        self, reference_id: str, instructions: str, config: object | None = None
    ) -> PromptFragments:
        return PromptFragments(
            read_context=(
                f"Wczytaj pull request {reference_id} (bitbucket_get_pr) — sprawdź obecny stan i czy nadal "
                "jest OTWARTY przed jakąkolwiek akcją."
            ),
            respond="Wynik przekaż w komentarzu do PR (bitbucket_add_comment).",
            transition_done=(
                "Oznacz rozwiązane wątki jako resolved (bitbucket_resolve_comment) i — jeśli właściwe — ustaw "
                "decyzję recenzji (bitbucket_set_review). Tylko dla włączonych narzędzi."
            ),
            extra=(
                "KONTEKST — instrukcje z momentu planowania:\n"
                "---\n"
                f"{instructions}\n"
                "---"
            ),
        )


class BitbucketCommentsChannel(BitbucketPromptChannel):
    """Sweep lane: open PRs with unanswered reviewer feedback."""

    _source = SOURCE_COMMENTS
    _intro = "rozwiąż nieodpowiedziany feedback recenzentów"


class BitbucketChangesChannel(BitbucketPromptChannel):
    """Fast lane: a reviewer requested changes on the agent's own open PR."""

    _source = SOURCE_CHANGES
    _intro = "wprowadź żądane przez recenzenta zmiany"


class BitbucketPublisher:
    """Publisher concern: decide (via the pure review_scan functions) whether a PR has work for a lane
    and, if so, publish exactly ONE job for it. Holds no token — the per-PR records come from the toolbox.
    """

    @staticmethod
    def reference_id(pr: dict) -> str:
        """``{workspace}/{repo}:{pr_id}`` — re-fetchable, stable per PR."""
        return f"{pr['workspace']}/{pr['repo']}:{pr['id']}"

    def publish_pr(
        self,
        db_config: object,
        pr: dict,
        *,
        lane: str,
        agent_view_id: int,
        priority: int,
        account_uuid: str,
        logger: logging.Logger | None = None,
    ) -> bool:
        ref = self.reference_id(pr)

        if lane == LANE_COMMENTS:
            unanswered = flag_unanswered(pr, account_uuid)
            if not unanswered:
                return False
            newest = unanswered[-1]  # flag_unanswered returns chronological order; last = newest
            reviewer_uuid = newest.get("author_uuid")
            idempotency_key = build_comments_key(ref, newest.get("created_on"))
            source = SOURCE_COMMENTS
            pub_priority = priority
            basis = "comments"
        elif lane == LANE_CHANGES:
            event = detect_changes_requested(pr, account_uuid)
            if event is None:
                return False
            reviewer_uuid = event.get("user_uuid")
            idempotency_key = build_changes_key(ref, event.get("date"))
            source = SOURCE_CHANGES
            pub_priority = min(100, priority + CHANGES_PRIORITY_BUMP)
            basis = "changes"
        else:
            raise ValueError(f"Unknown bitbucket lane: {lane!r}")

        # ACCOUNT trust: the reviewer identity comes from the authenticated Bitbucket API, not a self-claim.
        requester = JobRequester(
            key=f"bitbucket:account:{reviewer_uuid}",
            email=None,
            trust=RequesterTrust.ACCOUNT,
            meta={"basis": basis, "pr": ref, "reviewer_uuid": reviewer_uuid},
        )
        return publish(
            db_config,
            AgentType.TODO,
            source,
            idempotency_key,
            reference_id=ref,
            logger=logger,
            agent_view_id=agent_view_id,
            priority=pub_priority,
            skip_if_active=True,
            requester=requester,
        )
