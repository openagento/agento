"""Regression tests for impl review round 7.

The security finding here is the third pass over the same theme (argv → prompt in logs →
now stderr and the failure exception). Each pass moved content out of a *different* sink;
this one finishes the job by giving content exactly one destination: ``job.output``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.harness import RunRequest, clear
from tests.harness_fixtures import make_runner, register_builtin_harnesses

REPO = Path(__file__).resolve().parents[4]

STDERR_SENTINEL = "STDERR-CUSTOMER-DATA-9a2f"
STDOUT_SENTINEL = "STDOUT-CUSTOMER-DATA-4c1b"


class TestNoAgentContentInLogsOrErrorMessage:
    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def _failing_runner(self):
        runner = make_runner("claude", credential=None, credential_required=False)
        runner._record_usage = MagicMock()
        runner.logger = logging.getLogger("round7-log-test")
        runner._execute_process = MagicMock(
            return_value=MagicMock(
                returncode=2, stdout=STDOUT_SENTINEL, stderr=STDERR_SENTINEL,
            ),
        )
        return runner

    def test_stderr_is_never_logged(self, caplog):
        runner = self._failing_runner()
        with caplog.at_level(logging.DEBUG, logger="round7-log-test"), pytest.raises(RuntimeError):
            runner.execute(RunRequest(prompt="p"))

        assert STDERR_SENTINEL not in caplog.text

    def test_failed_stdout_is_never_logged(self, caplog):
        runner = self._failing_runner()
        with caplog.at_level(logging.DEBUG, logger="round7-log-test"), pytest.raises(RuntimeError):
            runner.execute(RunRequest(prompt="p"))

        assert STDOUT_SENTINEL not in caplog.text

    def test_byte_counts_still_logged(self, caplog):
        """Sanitizing must not cost observability — lengths stay."""
        runner = self._failing_runner()
        with caplog.at_level(logging.INFO, logger="round7-log-test"), pytest.raises(RuntimeError):
            runner.execute(RunRequest(prompt="p"))

        assert f"stderr={len(STDERR_SENTINEL)}b" in caplog.text
        assert "rc=2" in caplog.text

    def test_exception_message_carries_metadata_only(self):
        runner = self._failing_runner()
        with pytest.raises(RuntimeError) as exc:
            runner.execute(RunRequest(prompt="p"))

        message = str(exc.value)
        assert STDOUT_SENTINEL not in message
        assert STDERR_SENTINEL not in message
        assert "exited with code 2" in message
        assert "job.output" in message  # tells an operator where the content went

    def test_agent_output_rides_on_the_exception_for_persistence(self):
        """Nothing is lost: the consumer stores this in `job.output`, and an operator now
        gets the FULL output instead of a 500-char excerpt inside an error string."""
        runner = self._failing_runner()
        with pytest.raises(RuntimeError) as exc:
            runner.execute(RunRequest(prompt="p"))

        assert STDOUT_SENTINEL in getattr(exc.value, "agent_output", "")

    def test_consumer_persists_agent_output_on_failure(self):
        """The failure UPDATEs previously wrote only error_message/error_class, which is why
        removing content from the message would have blinded operators."""
        from agento.framework import consumer

        source = Path(consumer.__file__).read_text()
        assert 'agent_output = getattr(error, "agent_output", None)' in source
        # All failure branches (retry, usage-limit pool-wait, and dead-letter) must store it.
        assert source.count("output = COALESCE(%s, output)") == 3


class TestCredentialErrorLogsCarryNoContent:
    """`mark_credential_error`'s message is derived from CLI stderr by the harness's error
    classifier, so it can echo prompt or customer content."""

    def test_mark_error_logs_length_not_message(self):
        from agento.framework.agent_manager.credential_store import mark_credential_error

        cursor = MagicMock()
        cursor.rowcount = 1
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda _s: cursor
        conn.cursor.return_value.__exit__ = lambda *_a: False
        logger = MagicMock()

        mark_credential_error(conn, 1, f"401 for {STDERR_SENTINEL}", logger=logger)

        logged = " ".join(str(c) for c in logger.warning.call_args_list)
        assert STDERR_SENTINEL not in logged
        assert "msg_len=" in logged

    def test_throttle_logs_length_not_message(self):
        from datetime import datetime

        from agento.framework.agent_manager.credential_store import throttle_credential

        cursor = MagicMock()
        cursor.rowcount = 1
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda _s: cursor
        conn.cursor.return_value.__exit__ = lambda *_a: False
        logger = MagicMock()

        throttle_credential(
            conn, 1, datetime(2026, 1, 1), f"limit {STDERR_SENTINEL}", logger=logger,
        )

        logged = " ".join(str(c) for c in logger.info.call_args_list) + " ".join(
            str(c) for c in logger.warning.call_args_list
        )
        assert STDERR_SENTINEL not in logged


class TestModuleToggleReprovisionsTheSandbox:
    """`module:enable` changed Python state only. A harness module also declares the CLI
    binary the sandbox image installs, so enabling a third harness registered its adapter
    while its binary was absent, and disabling codex left codex installed."""

    def test_harness_module_declares_a_pin(self):
        from agento.framework.cli.module import _declared_sandbox_pins

        assert _declared_sandbox_pins(REPO, "codex") == {"CODEX_VERSION"}
        assert _declared_sandbox_pins(REPO, "claude") == {"CLAUDE_CODE_VERSION"}

    def test_non_harness_module_declares_none(self):
        """So toggling jira must not trigger an image rebuild."""
        from agento.framework.cli.module import _declared_sandbox_pins

        assert _declared_sandbox_pins(REPO, "jira") == set()

    def test_unknown_module_is_not_an_error(self):
        from agento.framework.cli.module import _declared_sandbox_pins

        assert _declared_sandbox_pins(REPO, "does_not_exist") == set()

    def _project(self, tmp_path):
        """A minimal installed project: managed compose builds .agento/docker/sandbox."""
        (tmp_path / ".agento").mkdir()
        (tmp_path / ".agento" / "project.json").write_text('{"name": "p"}')
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\n')
        return tmp_path

    def _drive_toggle(self, tmp_path, *, pins, enabled, source="local"):
        """Drive the real `_set_module_state`, stubbing only docker and module state."""
        from agento.framework.cli import module as mod

        calls: list[list[str]] = []
        with (
            patch.object(mod, "_declared_sandbox_pins", return_value=pins),
            patch("agento.framework.cli._project.find_project_root", return_value=tmp_path),
            patch(
                "agento.framework.cli._project.compose_file_flags",
                return_value=["-f", "c.yml"],
            ),
            patch("agento.framework.module_status.resolve_module_source",
                  return_value=source),
            patch("agento.framework.module_status.set_enabled"),
            patch("agento.framework.cli._provisioning.regenerate_compose"),
            patch.object(
                mod.subprocess, "run",
                side_effect=lambda argv, **kw: calls.append(argv)
                or MagicMock(returncode=0),
            ),
        ):
            mod._set_module_state("codex" if pins else "jira", enabled)
        return [" ".join(c) for c in calls]

    def test_toggle_renders_the_project_context_docker_builds(self, tmp_path):
        """Round 8: my round-7 fix rendered the IN-PACKAGE Dockerfile, but managed compose
        builds `<project>/.agento/docker/sandbox` — so the rebuild used a stale image. Assert
        the real file appears and carries the declared pins."""
        project = self._project(tmp_path)

        joined = self._drive_toggle(project, pins={"CODEX_VERSION"}, enabled=False)

        dockerfile = project / ".agento" / "docker" / "sandbox" / "Dockerfile"
        assert dockerfile.is_file(), "the project build context was never materialized"
        text = dockerfile.read_text()
        assert "ARG CODEX_VERSION=" in text
        assert "{{" not in text, "markers must be rendered, not shipped"
        assert any("build sandbox" in c for c in joined), joined
        assert any("up -d" in c for c in joined), joined

    def test_toggle_does_not_touch_docker_for_a_non_harness_local_module(self, tmp_path):
        project = self._project(tmp_path)

        joined = self._drive_toggle(project, pins=set(), enabled=True)

        assert not (project / ".agento" / "docker").exists()
        assert joined == []


class TestManifestBooleansAreValidatedNotCoerced:
    """`bool(v)` read the JSON string `"false"` as True — silently advertising a capability
    the harness does not have, so the framework branches on it and fails at runtime."""

    def _decl(self, **over):
        base = {
            "id": "h", "label": "H", "default_provider": "p",
            "providers": [{"id": "p", "credential_required": False}],
        }
        base.update(over)
        return base

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None, ""])
    def test_non_boolean_capability_is_rejected(self, value):
        from agento.framework.harness import HarnessDescriptor

        with pytest.raises(ValueError, match="must be true or false"):
            HarnessDescriptor.from_declaration(
                self._decl(capabilities={"interactive": value})
            )

    def test_real_booleans_are_accepted(self):
        from agento.framework.harness import HarnessDescriptor

        d = HarnessDescriptor.from_declaration(
            self._decl(capabilities={"interactive": True, "resume": False})
        )
        assert d.capabilities.interactive is True
        assert d.capabilities.resume is False

    @pytest.mark.parametrize("value", [[1], "nope", 3])
    def test_non_object_capabilities_is_a_manifest_error(self, value):
        from agento.framework.harness import HarnessDescriptor

        with pytest.raises(ValueError, match="capabilities must be an object"):
            HarnessDescriptor.from_declaration(self._decl(capabilities=value))

    @pytest.mark.parametrize("value", ["nope", 3, [1]])
    def test_non_object_sandbox_package_is_a_manifest_error(self, value):
        """Previously escaped as TypeError/AttributeError from deep inside parsing."""
        from agento.framework.harness import HarnessDescriptor

        with pytest.raises(ValueError, match="sandbox_package must be an object"):
            HarnessDescriptor.from_declaration(self._decl(sandbox_package=value))

    @pytest.mark.parametrize("value", ["false", "true", 1, 0])
    def test_non_boolean_credential_required_is_rejected(self, value):
        """The most dangerous coercion: `"false"` would mark a provider as needing a
        credential (or not) against the declaration's plain meaning."""
        from agento.framework.harness import HarnessDescriptor

        with pytest.raises(ValueError, match="credential_required must be true or false"):
            HarnessDescriptor.from_declaration(
                self._decl(providers=[{"id": "p", "credential_required": value}])
            )


class TestCliMigrationCompleted:
    def test_replay_flag_matches_the_plan(self):
        """The plan specifies `--credential`; the error text already said so."""
        import argparse

        from agento.framework.cli.runtime import ReplayCommand

        parser = argparse.ArgumentParser()
        ReplayCommand().configure(parser)

        for flag in ("--credential", "--credential-id", "--oauth_token"):
            ns = parser.parse_args(["7", flag, "5"])
            assert ns.credential_id == 5, flag

    def test_list_and_usage_validate_the_scope(self):
        from agento.framework.cli import credential

        source = Path(credential.__file__).read_text()
        # register already validated it; list and usage now do too.
        assert source.count("_validate_scope(args.scope)") == 3

    def test_credential_domain_helper_is_renamed(self):
        from agento.framework.cli import runtime

        assert hasattr(runtime, "_resolve_credential")
        assert not hasattr(runtime, "_resolve_token")

    def test_operator_messages_say_credential(self):
        from agento.framework.cli import runtime

        source = Path(runtime.__file__).read_text()
        assert "Token not found" not in source
        assert "Token disabled" not in source
        assert "Credential not found" in source

    def test_admin_helper_is_renamed(self):
        from agento.framework.admin import data

        assert hasattr(data, "do_reset_credential_error")
        assert not hasattr(data, "do_reset_token_error")


class TestEqualOriginPrecedenceIsDocumentedCorrectly:
    """The doc said the harness wins on equal origins; the implementation and tests make the
    legacy provider win. Subtle rules are exactly where docs must not drift."""

    def test_doc_matches_the_implementation(self):
        doc = (REPO / "docs" / "architecture" / "harness-contract.md").read_text()

        assert "On a tie the **legacy provider wins**" in doc
        assert "strictly stronger" in doc
        assert "provider_origin >=" in doc  # names the actual condition

    def test_implementation_still_uses_that_condition(self):
        from agento.framework import agent_view_runtime

        source = Path(agent_view_runtime.__file__).read_text()
        assert "provider_origin >= harness_origin" in source
