from __future__ import annotations

import argparse
import json
import logging
import sys

from ..agent_manager.config import AgentManagerConfig
from ..agent_manager.credential_store import get_credential, select_credential
from ..consumer_config import ConsumerConfig
from ..database_config import DatabaseConfig
from ..db import get_connection_or_exit
from ..log import get_logger


def _load_framework_config() -> tuple[DatabaseConfig, ConsumerConfig, AgentManagerConfig]:
    """Load framework-level config from env vars.

    Returns (DatabaseConfig, ConsumerConfig, AgentManagerConfig).
    For commands that don't need module config -- just DB access and framework tuning.
    """
    return (
        DatabaseConfig.from_env(),
        ConsumerConfig.from_env(),
        AgentManagerConfig.from_env(),
    )


def _resolve_credential(credential_id: int | None = None, *, scope: str | None = None):
    """Resolve a credential by explicit id, or claim the LRU healthy one from its pool.

    When ``credential_id`` is None a ``scope`` must be supplied; replay derives it from the
    job record's harness.
    """

    db_config, _, _ = _load_framework_config()
    conn = get_connection_or_exit(db_config)
    try:
        if credential_id is not None:
            credential = get_credential(conn, credential_id)
            if credential is None:
                raise ValueError(f"Credential not found: id={credential_id}")
            if not credential.enabled:
                raise ValueError(f"Credential disabled: id={credential_id}")
            return credential
        if scope is None:
            raise RuntimeError(
                "Cannot resolve a credential without --credential or a scope. "
                "Pass --credential or configure agent_view/harness."
            )
        selected = select_credential(conn, scope)
        if selected is None:
            raise RuntimeError(
                f"No healthy credentials for scope={scope}. "
                f"Check: bin/agento credential:list --all"
            )
        return selected
    finally:
        conn.close()


def _harness_and_provider_for_scope(scope: str) -> tuple[str, str]:
    """Map a credential scope to the ``(harness, provider)`` that owns it.

    A scope is NOT a harness id — the axes are independent, and a harness may name its
    scope anything. Resolve the owner through the registry instead of assuming they
    coincide (they happen to for the two shipped harnesses, which is what made the
    assumption look harmless).
    """
    from ..harness import get_harness_for_scope

    registered = get_harness_for_scope(scope)
    if registered is None:
        print(
            f"Error: no registered harness owns credential scope {scope!r}.",
            file=sys.stderr,
        )
        sys.exit(1)
    descriptor = registered.descriptor
    provider = next(
        (p for p in descriptor.providers if p.credential_scope == scope), None,
    )
    if provider is None:  # pragma: no cover - registry guarantees the inverse mapping
        print(
            f"Error: harness {descriptor.id!r} owns scope {scope!r} but offers no "
            f"provider using it.",
            file=sys.stderr,
        )
        sys.exit(1)
    return str(descriptor.id), str(provider.id)


def _make_runner(
    harness: str,
    provider: str,
    logger: logging.Logger | None = None,
    *,
    credential=None,
    model: str | None = None,
) -> object:
    """Build a runner for an explicit (harness, provider), claiming the credential ONCE.

    The caller owns credential selection (the runner has no pool access), so the command
    and the process can never end up on two different credentials. Pass ``credential``
    when it has already been claimed (e.g. ``replay --credential-id``) so this does not
    claim a second, different one.
    """
    from ..harness import HarnessRunContext, create_runner, resolve_provider

    provider_desc = resolve_provider(harness, provider)
    if credential is None and provider_desc.credential_required:
        credential = _resolve_credential(scope=provider_desc.credential_scope)
    _, consumer_config, _ = _load_framework_config()
    ctx = HarnessRunContext(
        harness=harness,
        provider=provider_desc.id,
        model=model,
        credential_required=provider_desc.credential_required,
        credential=credential,
    )
    return create_runner(harness, ctx, logger=logger, dry_run=consumer_config.disable_llm)


class ConsumerCommand:
    @property
    def name(self) -> str:
        return "consumer"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Start the job consumer"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        from ..bootstrap import bootstrap
        from ..consumer import Consumer

        db_config, consumer_config, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            bootstrap(db_conn=conn)
        finally:
            conn.close()

        logger = get_logger("consumer", "/app/logs/consumer.log")
        consumer = Consumer(db_config, consumer_config, logger)
        consumer.run()


class SetupUpgradeCommand:
    @property
    def name(self) -> str:
        return "setup:upgrade"

    @property
    def shortcut(self) -> str:
        return "se:up"

    @property
    def help(self) -> str:
        return "Apply schema migrations, data patches, and install crontab"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Show pending work without applying")
        parser.add_argument("--skip-onboarding", action="store_true", dest="skip_onboarding",
                         help="Skip interactive module onboarding prompts")

    def execute(self, args: argparse.Namespace) -> None:
        import sys

        from ..dependency_resolver import DisabledDependencyError
        from ..setup import ModuleValidationError, setup_upgrade

        db_config, _, _ = _load_framework_config()
        logger = get_logger("setup")
        conn = get_connection_or_exit(db_config)
        try:
            try:
                skip_onboarding = getattr(args, "skip_onboarding", False)
                result = setup_upgrade(
                    conn, logger, dry_run=args.dry_run, skip_onboarding=skip_onboarding,
                )
            except (DisabledDependencyError, ModuleValidationError) as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

            if args.dry_run:
                if not result.has_work:
                    print("Nothing to do.")
                    return
                print("Pending setup work:\n")
                if result.framework_migrations:
                    print(f"  Framework migrations ({len(result.framework_migrations)}):")
                    for v in result.framework_migrations:
                        print(f"    {v}")
                for mod, versions in result.module_migrations.items():
                    print(f"  Module migrations [{mod}] ({len(versions)}):")
                    for v in versions:
                        print(f"    {v}")
                for mod, patches in result.data_patches.items():
                    print(f"  Data patches [{mod}] ({len(patches)}):")
                    for p in patches:
                        print(f"    {p}")
                if result.cron_changed:
                    print("  Crontab: would be updated")
            else:
                if not result.has_work:
                    print("Nothing to do.")
                    return
                if result.framework_migrations:
                    print(f"Applied {len(result.framework_migrations)} framework migration(s)")
                for mod, versions in result.module_migrations.items():
                    print(f"Applied {len(versions)} migration(s) for {mod}")
                for mod, patches in result.data_patches.items():
                    print(f"Applied {len(patches)} data patch(es) for {mod}")
                if result.cron_changed:
                    print("Crontab updated")
                for mod in result.onboardings_run:
                    print(f"Onboarding completed for {mod}")
                if result.onboardings_disabled:
                    print(f"Modules disabled during onboarding: {', '.join(result.onboardings_disabled)}")
        finally:
            conn.close()


class ReplayCommand:
    @property
    def name(self) -> str:
        return "replay"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Replay a job by ID"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=int, help="Job ID to replay")
        parser.add_argument("--credential", "--credential-id", "--oauth_token",
                          type=int, dest="credential_id", default=None,
                          help="Override credential id (default: least-recently-used healthy credential)")
        parser.add_argument("--model", type=str, default=None,
                          help="Override the model (e.g. claude-opus-4-20250514)")
        parser.add_argument("--exec", action="store_true",
                          help="Actually execute the command (not just display)")
        parser.add_argument("--json", action="store_true",
                          help="Output in JSON format")

    def execute(self, args: argparse.Namespace) -> None:
        from ..bootstrap import bootstrap
        from ..replay import build_replay_command, fetch_job_for_replay

        db_config, _consumer_config, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            bootstrap(db_conn=conn)
        finally:
            conn.close()

        job = fetch_job_for_replay(args.job_id, db_config)

        # An explicit --credential-id also pins the harness: resolve the scope's OWNER
        # rather than treating the scope string as a harness id.
        # An explicit --credential-id also pins the harness AND the provider: resolve the
        # scope's OWNER rather than treating the scope string as a harness id.
        credential = _resolve_credential(args.credential_id) if args.credential_id else None
        harness_override = provider_override = None
        if credential is not None:
            harness_override, provider_override = _harness_and_provider_for_scope(
                credential.scope
            )

        replay = build_replay_command(
            job,
            harness_override=harness_override,
            provider_override=provider_override,
            model_override=args.model,
        )

        if args.exec:
            logger = get_logger("replay", "/app/logs/replay.log", stderr=False)
            from ..harness import RunRequest

            # `replay.provider`/`replay.model` already resolved override → job value →
            # default. Passing `args.model` here instead meant a replay without --model
            # DISPLAYED the job's model but EXECUTED on the provider default.
            runner = _make_runner(
                replay.harness, replay.provider, logger=logger, credential=credential,
                model=replay.model,
            )
            result = runner.execute(
                RunRequest(prompt=replay.prompt, model=replay.model)
            )
            print(json.dumps({
                "job_id": job.id,
                "agent_type": result.harness or replay.harness,
                "provider": result.provider or replay.provider,
                "model": result.model or replay.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "duration_ms": result.duration_ms,
                "session_id": result.session_id,
                "output_preview": result.raw_output[:500],
            }, indent=2))
        elif args.json:
            print(json.dumps({
                "job_id": job.id,
                "type": job.type.value,
                "source": job.source,
                "reference_id": job.reference_id,
                "agent_type": replay.harness,
                "provider": replay.provider,
                "model": replay.model,
                "command": replay.args,
                "shell_command": replay.shell_command,
                "prompt_length": len(replay.prompt),
                "prompt_preview": replay.prompt[:200],
            }, indent=2, ensure_ascii=False))
        else:
            print(f"Job #{job.id} ({job.type.value}) ref={job.reference_id}")
            print(f"Agent: {replay.harness}  Model: {replay.model or 'default'}")
            print(f"Prompt ({len(replay.prompt)} chars):")
            print("---")
            print(replay.prompt)
            print("---")
            print()
            print("Command:")
            print(f"  {replay.shell_command}")


class PauseCommand:
    @property
    def name(self) -> str:
        return "job:pause"

    @property
    def shortcut(self) -> str:
        return "jo:pa"

    @property
    def help(self) -> str:
        return "Pause a running job (SIGTERM + keep session)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=int, help="Job ID to pause")

    def execute(self, args: argparse.Namespace) -> None:
        import sys

        from ..event_manager import get_event_manager
        from ..events import JobPausedEvent
        from ..job_store import pause_job

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            job = pause_job(conn, args.job_id)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            conn.close()

        get_event_manager().dispatch("job_pause_after", JobPausedEvent(job=job))

        print(f"Job #{job.id} paused.")
        if job.session_id:
            print(f"  session_id: {job.session_id}")
        print(f"  pid: {job.pid or 'N/A'}")
        print()
        print("Resume with: agento job:resume", job.id)


class ResumeCommand:
    @property
    def name(self) -> str:
        return "job:resume"

    @property
    def shortcut(self) -> str:
        return "jo:re"

    @property
    def help(self) -> str:
        return "Resume a paused job (re-queue for consumer pickup)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=int, help="Job ID to resume")

    def execute(self, args: argparse.Namespace) -> None:
        import sys

        from ..event_manager import get_event_manager
        from ..events import JobResumedEvent
        from ..job_store import resume_job

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            job = resume_job(conn, args.job_id)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            conn.close()

        get_event_manager().dispatch("job_resume_after", JobResumedEvent(job=job))

        print(f"Job #{job.id} re-queued.")
        print(f"  session_id: {job.session_id}")
        print()
        print("Job will be picked up by the next consumer poll;")
        print(f"it will resume via session_id={job.session_id}")


class JobListCommand:
    @property
    def name(self) -> str:
        return "job:list"

    @property
    def shortcut(self) -> str:
        return "jo:li"

    @property
    def help(self) -> str:
        return "List recent jobs (--status/--source/--agent-view) — surfaces failed/dead jobs"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--status",
            choices=["TODO", "RUNNING", "SUCCESS", "FAILED", "DEAD", "PAUSED"],
            default=None,
            help="Filter by job status (e.g. DEAD for dead-lettered failures)",
        )
        parser.add_argument("--source", default=None, help="Filter by job source (e.g. outlook, jira)")
        parser.add_argument("--agent-view", dest="agent_view", default=None,
                            help="Filter by agent_view code")
        parser.add_argument("--limit", type=int, default=20, help="Max rows to show (default 20)")

    def execute(self, args: argparse.Namespace) -> None:
        import sys
        from datetime import datetime

        from ..admin.data import get_jobs

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            # strict=True: surface a query failure instead of an empty list — this command exists to make
            # failures visible, so a silent "no jobs" on a broken query would defeat its purpose.
            rows = get_jobs(
                conn,
                limit=args.limit,
                status=args.status,
                source=args.source,
                agent_view_code=args.agent_view,
                strict=True,
            )
        except Exception as e:
            print(f"Error: could not query jobs: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            conn.close()

        if not rows:
            print("No jobs found.")
            return

        header = (f"{'ID':>6}  {'STATUS':<8}  {'SOURCE':<10}  {'TYPE':<8}  "
                  f"{'AGENT_VIEW':<14}  {'REFERENCE':<20}  CREATED")
        print(header)
        print("-" * len(header))
        for r in rows:
            created = r.get("created_at")
            created_s = created.strftime("%Y-%m-%d %H:%M") if isinstance(created, datetime) else str(created or "")
            print(
                f"{r.get('id', ''):>6}  {(r.get('status') or ''):<8}  {(r.get('source') or ''):<10}  "
                f"{(r.get('type') or ''):<8}  {(r.get('agent_view_code') or '-'):<14}  "
                f"{str(r.get('reference_id') or '-')[:20]:<20}  {created_s}"
            )
            if (r.get("status") or "").upper() in ("FAILED", "DEAD"):
                ec = r.get("error_class") or ""
                em = (r.get("error_message") or "").replace("\n", " ")
                if len(em) > 100:
                    em = em[:100] + "…"
                if ec or em:
                    print(f"        ↳ {ec}: {em}" if ec else f"        ↳ {em}")


class E2eCommand:
    @property
    def name(self) -> str:
        return "e2e"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Run end-to-end tests with real LLM calls"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--credential", "--credential-id", "--oauth_token",
                          type=int, dest="credential_id", default=None,
                          help="Override credential id (default: least-recently-used healthy credential)")
        parser.add_argument("--keep", action="store_true",
                          help="Keep test jobs in DB (don't clean up)")
        parser.add_argument("--model", type=str, default=None,
                          help="Override the model (e.g. claude-opus-4-20250514)")

    def execute(self, args: argparse.Namespace) -> None:
        from ..e2e import cmd_e2e

        cmd_e2e(args)
