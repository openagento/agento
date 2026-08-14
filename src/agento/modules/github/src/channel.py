from __future__ import annotations

import logging

from agento.framework.channels.base import PromptFragments
from agento.framework.job_models import AgentType, JobRequester, RequesterTrust
from agento.framework.publisher import publish

from .review_scan import (
    build_changes_key,
    build_comments_key,
    comments_key_parts,
    detect_changes_requested,
    flag_unanswered,
    lane_data_is_complete,
)

# Lane tokens (the toolbox `lane` arg + publish_pr selector).
LANE_COMMENTS = "comments"
LANE_CHANGES = "changes"

# Published job.source per lane. These MUST equal the registered channel `.name` values below, because
# the framework resolves a job's prompt channel via get_channel(job.source) keyed on the instance .name
# — and distinct sources are what make skip_if_active dedup the two lanes independently.
SOURCE_COMMENTS = "github-comments"
SOURCE_CHANGES = "github-changes"

# Fast lane (changes-requested) is prioritized above the view's base priority; capped at 100 because the
# consumer claims by `priority DESC` (higher = sooner).
CHANGES_PRIORITY_BUMP = 30


class GitHubPromptChannel:
    """Channel concern: Polish prompt fragments for a PR-review task.

    Subclassed per lane; they differ only in the one-line intro. The agent must FIRST read the PR and
    confirm it is still OPEN before doing anything; the toolbox write tools enforce the same gate as
    defence-in-depth.
    """

    _source: str = ""
    _intro: str = ""

    @property
    def name(self) -> str:
        return self._source

    def get_prompt_fragments(self, reference_id: str) -> PromptFragments:
        return PromptFragments(
            read_context=(
                f"Zadanie code-review dla pull requesta {reference_id} ({self._intro}).\n"
                f"NAJPIERW pobierz PR (github_get_pr) i sprawdź, czy nadal jest OTWARTY (state == open). "
                "Jeśli jest zmergowany/zamknięty — ZAKOŃCZ czysto, bez komentarzy i bez push.\n"
                "Następnie wczytaj diff (github_get_pr_diff), komentarze (github_get_pr_comments) "
                "i historię recenzji (github_get_pr_reviews)."
            ),
            respond=(
                "Odnieś się do feedbacku recenzentów: odpowiedz na komentarze (github_add_comment, w razie "
                "potrzeby inline na konkretnej linii pliku). Jeśli wymagane są zmiany w kodzie, wypchnij commity "
                "na gałąź źródłową PR przy użyciu tożsamości git workspace'u."
            ),
            transition_done=(
                "Po rozwiązaniu wątku oznacz go jako resolved (github_resolve_thread); jeśli to właściwe, "
                "prześlij recenzję (github_set_review). Wykonuj wyłącznie te akcje, których narzędzia są "
                "włączone."
            ),
            ask_and_handback=(
                "Jeśli masz pytania lub wątpliwości:\n"
                "  a) Zadaj je w komentarzu do PR (github_add_comment).\n"
                "  b) ZAKOŃCZ — nie wykonuj dalszych kroków.\n"
                "Jeśli wcześniej zadałeś pytania i nie ma odpowiedzi: ZAKOŃCZ."
            ),
        )

    def get_followup_fragments(self, reference_id: str, instructions: str) -> PromptFragments:
        return PromptFragments(
            read_context=(
                f"Wczytaj pull request {reference_id} (github_get_pr) — sprawdź obecny stan i czy nadal "
                "jest OTWARTY przed jakąkolwiek akcją."
            ),
            respond="Wynik przekaż w komentarzu do PR (github_add_comment).",
            transition_done=(
                "Oznacz rozwiązane wątki jako resolved (github_resolve_thread) i — jeśli właściwe — prześlij "
                "recenzję (github_set_review). Tylko dla włączonych narzędzi."
            ),
            extra=(
                "KONTEKST — instrukcje z momentu planowania:\n"
                "---\n"
                f"{instructions}\n"
                "---"
            ),
        )


class GitHubCommentsChannel(GitHubPromptChannel):
    """Sweep lane: open PRs with unanswered reviewer feedback."""

    _source = SOURCE_COMMENTS
    _intro = "rozwiąż nieodpowiedziany feedback recenzentów"


class GitHubChangesChannel(GitHubPromptChannel):
    """Fast lane: a reviewer requested changes on the agent's own open PR."""

    _source = SOURCE_CHANGES
    _intro = "wprowadź żądane przez recenzenta zmiany"


class GitHubPublisher:
    """Publisher concern: decide (via the pure review_scan functions) whether a PR has work for a lane
    and, if so, publish exactly ONE job for it. Holds no token — the per-PR records come from the toolbox.
    """

    @staticmethod
    def reference_id(pr: dict) -> str:
        """``{owner}/{repo}:{number}`` — re-fetchable, stable per PR."""
        return f"{pr['owner']}/{pr['repo']}:{pr['id']}"

    def publish_pr(
        self,
        db_config: object,
        pr: dict,
        *,
        lane: str,
        agent_view_id: int,
        priority: int,
        login: str,
        logger: logging.Logger | None = None,
    ) -> bool:
        ref = self.reference_id(pr)

        # A truncated scan the lane depends on means "unknown", not "nothing to do" — publishing from
        # partial data can enqueue work that was already resolved. Skip; the next poll re-detects it.
        if not lane_data_is_complete(pr, lane):
            if logger:
                logger.warning(
                    "github: incomplete scan for %s lane=%s (truncated: %s) — not publishing",
                    ref, lane, ", ".join(pr.get("truncated") or ()),
                )
            return False

        if lane == LANE_COMMENTS:
            unanswered = flag_unanswered(pr, login)
            if not unanswered:
                return False
            newest = unanswered[-1]  # flag_unanswered returns a total order; last = newest
            reviewer_login = newest.get("author_login")
            created_at, identities = comments_key_parts(unanswered)
            idempotency_key = build_comments_key(ref, created_at, identities)
            source = SOURCE_COMMENTS
            pub_priority = priority
            basis = "comments"
        elif lane == LANE_CHANGES:
            event = detect_changes_requested(pr, login)
            if event is None:
                return False
            reviewer_login = event.get("user_login")
            idempotency_key = build_changes_key(ref, event.get("date"), event.get("id"))
            source = SOURCE_CHANGES
            pub_priority = min(100, priority + CHANGES_PRIORITY_BUMP)
            basis = "changes"
        else:
            raise ValueError(f"Unknown github lane: {lane!r}")

        # No identity ⇒ no job. ACCOUNT trust asserts "the API told us who this is"; a deleted/ghost
        # account (user: null) would otherwise become the requester key `github:login:None` (G-17).
        if not reviewer_login:
            return False

        # ACCOUNT trust: the reviewer identity comes from the authenticated GitHub API, not a self-claim.
        requester = JobRequester(
            key=f"github:login:{reviewer_login}",
            email=None,
            trust=RequesterTrust.ACCOUNT,
            meta={"basis": basis, "pr": ref, "reviewer_login": reviewer_login},
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
