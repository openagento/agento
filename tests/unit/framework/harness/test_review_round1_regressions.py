"""Regression tests for the failure paths found in impl review round 1.

Each class pins one defect the happy-path suite did not exercise.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework.harness import (
    RunRequest,
    clear,
    parse_harness_declarations,
    register_harness,
)
from agento.framework.module_loader import import_class
from tests.harness_fixtures import make_runner, register_builtin_harnesses

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "modules"

SENTINEL = "SECRET-PROMPT-CONTENT-b7f3"


def _register_fake() -> None:
    module_dir = FIXTURES / "fake_harness"
    for decl in parse_harness_declarations(module_dir / "di.json", "fake_harness"):
        register_harness(decl.descriptor, import_class(module_dir, decl.class_path)(),
                             decl.module, decl.runtime_config_fields,
                             _module_config_schema(module_dir))


class TestPromptNeverReachesInfoLogs:
    """The argv carries the job prompt — Jira content, PII, whatever an operator pasted.
    INFO is the level the consumer actually runs at, so content must not land there."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def _run(self, level: int, caplog):
        runner = make_runner("claude", credential=None, credential_required=False)
        runner._execute_process = MagicMock(
            return_value=MagicMock(
                returncode=0,
                stdout='{"type":"result","result":"ok","usage":{}}\n',
                stderr="",
            ),
        )
        runner._record_usage = MagicMock()
        runner.logger = logging.getLogger("harness-log-test")
        with caplog.at_level(level, logger="harness-log-test"):
            runner.execute(RunRequest(prompt=SENTINEL))
        return caplog

    def test_prompt_is_absent_from_info_records(self, caplog):
        self._run(logging.INFO, caplog)
        assert SENTINEL not in caplog.text

    def test_metadata_is_still_logged_at_info(self, caplog):
        """Dropping the prompt must not cost observability."""
        self._run(logging.INFO, caplog)
        assert "bin=claude" in caplog.text
        assert f"prompt_len={len(SENTINEL)}" in caplog.text
        assert "rc=0" in caplog.text

    def test_argv_is_not_logged_at_any_level(self, caplog):
        """Round 1 kept the full argv at DEBUG, arguing opt-in verbosity justified it.
        Round 5 rejected that and I switched to redaction; round 10 showed redaction of
        untrusted argv is unsound at all (a builder can transform the prompt or carry its own
        secrets), so nothing argv-derived is logged. Pinned here as the final position.
        """
        self._run(logging.DEBUG, caplog)

        assert SENTINEL not in caplog.text
        assert "bin=claude" in caplog.text
        assert f"prompt_len={len(SENTINEL)}" in caplog.text


class TestScopeIsNotAHarnessId:
    """``credential.scope`` and the harness id are independent axes. Treating the scope
    string as a harness id happened to work for the two shipped harnesses (where they
    coincide) and breaks for any harness that names its scope differently."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        clear()
        _register_fake()
        yield
        clear()

    def test_scope_owner_is_resolved_through_the_registry(self):
        from agento.framework.cli.runtime import _harness_and_provider_for_scope

        # 'fake_cloud' is a SCOPE, not a harness — the owner is harness 'fake'.
        assert _harness_and_provider_for_scope("fake_cloud") == ("fake", "fake_cloud")

    def test_unknown_scope_exits_with_a_clear_message(self, capsys):
        from agento.framework.cli.runtime import _harness_and_provider_for_scope

        with pytest.raises(SystemExit) as exc:
            _harness_and_provider_for_scope("nobody_owns_this")

        assert exc.value.code == 1
        assert "no registered harness owns credential scope" in capsys.readouterr().err


class TestExplicitCredentialIsHonoured:
    """``replay --credential-id N`` must run on credential N. Claiming a second one from
    the pool would silently replay on a different license than the one requested."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def test_passed_credential_is_used_without_a_second_claim(self, monkeypatch):
        from agento.framework.cli import runtime as rt
        from tests.unit.agent_manager.conftest import make_token

        explicit = make_token(id=99, label="explicit", credentials={"api_key": "sk-X"})
        monkeypatch.setattr(
            rt, "_resolve_credential",
            lambda *a, **kw: pytest.fail("must not claim a second credential"),
        )
        monkeypatch.setattr(
            rt, "_load_framework_config",
            lambda: (MagicMock(), MagicMock(disable_llm=True), MagicMock()),
        )

        runner = rt._make_runner("claude", "anthropic", credential=explicit)

        assert runner.context.credential is explicit

    def test_pool_is_used_when_no_credential_is_passed(self, monkeypatch):
        from agento.framework.cli import runtime as rt
        from tests.unit.agent_manager.conftest import make_token

        pooled = make_token(id=1, label="pooled", credentials={"api_key": "sk-Y"})
        monkeypatch.setattr(rt, "_resolve_credential", lambda *a, **kw: pooled)
        monkeypatch.setattr(
            rt, "_load_framework_config",
            lambda: (MagicMock(), MagicMock(disable_llm=True), MagicMock()),
        )

        runner = rt._make_runner("claude", "anthropic")

        assert runner.context.credential is pooled


class TestRegistrationModesGateEveryFlow:
    """The DECLARATION decides which flows exist. ``fake_cloud`` declares only
    api_key/access_token, so interactive OAuth must be refused with the supported list —
    not dispatched into the authenticator's defensive raise, which nothing catches."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        clear()
        _register_fake()
        yield
        clear()

    def test_interactive_oauth_is_refused_for_a_secret_only_scope(self, capsys, monkeypatch):
        import argparse

        from agento.framework.cli.credential import _resolve_credentials

        args = argparse.Namespace(
            scope="fake_cloud", label="l",
            with_api_key=False, with_access_token=False, token_limit=0,
        )
        with pytest.raises(SystemExit) as exc:
            _resolve_credentials(args, "fake_cloud", MagicMock())

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "interactive OAuth is not supported" in err
        assert "api_key" in err and "access_token" in err

    def test_declared_secret_mode_still_works(self, monkeypatch):
        import argparse
        import io

        from agento.framework.cli.credential import _resolve_credentials

        args = argparse.Namespace(
            scope="fake_cloud", label="l",
            with_api_key=True, with_access_token=False, token_limit=0,
        )
        stdin = io.StringIO("sk-fake\n")
        stdin.isatty = lambda: False  # type: ignore[assignment]
        monkeypatch.setattr("sys.stdin", stdin)

        creds, type_ = _resolve_credentials(args, "fake_cloud", MagicMock())

        assert (creds, type_) == ({"api_key": "sk-fake"}, "fake_api_key")


class TestInteractiveRunToleratesAnEmptyPool:
    """Starting the CLI with no credential is how an operator reaches its own `/login`.
    Headless keeps failing fast — a prompt run without one only fails deeper in, after
    burning a session."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def _run(self, prompt):
        """Drive the real command with an EMPTY credential pool."""
        import argparse
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from agento.modules.agent_view.src.commands.prepare_run import (
            AgentViewPrepareRunCommand,
        )

        runtime = MagicMock()
        runtime.harness = "claude"
        runtime.provider = "anthropic"
        runtime.model = None
        runtime.workspace = MagicMock(id=3, code="acme")
        runtime.agent_view = MagicMock(id=7, code="dev")

        writer = MagicMock()
        writer.credential_env.return_value = {}

        resolver = MagicMock()
        resolver.resolve.side_effect = RuntimeError(
            "No enabled credentials for scope=claude"
        )

        args = argparse.Namespace(
            agent_view_code="dev", prompt=prompt, model=None, yolo=False,
        )
        with (
            patch("agento.framework.cli.runtime._load_framework_config",
                  return_value=(MagicMock(), MagicMock(), MagicMock())),
            patch("agento.framework.db.get_connection_or_exit", return_value=MagicMock()),
            patch("agento.framework.workspace.get_agent_view_by_code",
                  return_value=MagicMock(id=7, code="dev")),
            patch("agento.framework.agent_view_runtime.resolve_agent_view_runtime",
                  return_value=runtime),
            patch("agento.framework.agent_manager.credential_resolver.CredentialResolver",
                  return_value=resolver),
            patch("agento.framework.run_preparation.materialize_run_workspace",
                  return_value=(Path("/w/run"), Path("/w/run"))),
            patch("agento.framework.harness.workspace_adapter_for", return_value=writer),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                AgentViewPrepareRunCommand().execute(args)
            return json.loads(buf.getvalue())

    def test_interactive_proceeds_without_a_credential(self, capsys):
        payload = self._run(prompt=None)

        assert payload["credential_id"] is None
        assert payload["command"], "the CLI command must still be built for /login"
        assert payload["env"] == {}

    def test_interactive_warns_how_to_recover(self, capsys):
        self._run(prompt=None)
        err = capsys.readouterr().err
        assert "/login" in err
        assert "credential:register claude" in err

    def test_headless_still_fails_fast(self):
        with pytest.raises(SystemExit) as exc:
            self._run(prompt="do the thing")
        assert exc.value.code == 1


class TestCredentiallessUsageIsGroupedByBothAxes:
    """One harness can offer several credential-less providers; a single lumped total
    could not tell them apart."""

    def test_query_groups_by_harness_and_provider(self):
        from agento.framework.agent_manager.usage_store import get_credentialless_usage

        cursor = MagicMock()
        cursor.fetchall.return_value = [
            {"harness": "fake", "provider": "fake_local", "total_tokens": 30,
             "call_count": 2},
            {"harness": "other", "provider": "local", "total_tokens": 5, "call_count": 1},
        ]
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda _s: cursor
        conn.cursor.return_value.__exit__ = lambda *_a: False

        rows = get_credentialless_usage(conn, window_hours=24)

        sql = cursor.execute.call_args.args[0]
        assert "credential_id IS NULL" in sql
        assert "GROUP BY harness, provider" in sql
        assert [(r.harness, r.provider, r.total_tokens) for r in rows] == [
            ("fake", "fake_local", 30), ("other", "local", 5),
        ]

    def test_harness_filter_is_parameterized(self):
        from agento.framework.agent_manager.usage_store import get_credentialless_usage

        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda _s: cursor
        conn.cursor.return_value.__exit__ = lambda *_a: False

        get_credentialless_usage(conn, harness="fake", window_hours=12)

        sql, params = cursor.execute.call_args.args
        assert "AND harness = %s" in sql
        assert params == [12, "fake"]


class TestCollisionsAreNotSurvivable:
    """A duplicate credential scope would let one harness serve another's pool, so
    bootstrap must NOT swallow it the way it swallows a broken module's import error."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        clear()
        yield
        clear()

    def test_bootstrap_propagates_a_scope_collision(self):
        from agento.framework.bootstrap import _load_agent_harnesses
        from agento.framework.harness import DuplicateCredentialScopeError

        register_builtin_harnesses()  # claims scope 'claude'

        manifest = MagicMock()
        manifest.name = "scope_collision"
        manifest.path = FIXTURES / "scope_collision"

        with pytest.raises(DuplicateCredentialScopeError):
            _load_agent_harnesses(manifest)

    def test_bootstrap_still_tolerates_an_unimportable_adapter(self, tmp_path):
        """One broken third-party module must not take the consumer down."""
        import json

        from agento.framework.bootstrap import _load_agent_harnesses

        mod = tmp_path / "broken"
        mod.mkdir()
        (mod / "di.json").write_text(json.dumps({"agent_harnesses": [{
            "id": "broken", "label": "B", "class": "src.nope.Missing",
            "default_provider": "p",
            "providers": [{"id": "p", "credential_required": False}],
        }]}))

        manifest = MagicMock()
        manifest.name = "broken"
        manifest.path = mod

        _load_agent_harnesses(manifest)  # must not raise

        from agento.framework.harness import find_harness

        assert find_harness("broken") is None


class TestCrossModuleValidation:
    def test_duplicate_scope_across_modules_fails_validation(self, tmp_path):
        """Per-module validation cannot see another module's claims; `setup:upgrade` must
        abort before any schema change."""
        import shutil

        from agento.framework.module_validator import validate_all

        core = tmp_path / "core"
        core.mkdir()
        for name in ("claude", "scope_collision"):
            src = (
                Path("src/agento/modules/claude") if name == "claude"
                else FIXTURES / "scope_collision"
            )
            shutil.copytree(src, core / name)

        results = validate_all(core, tmp_path / "absent")

        errors = results.get("scope_collision", [])
        assert any("credential_scope 'claude'" in e for e in errors), results

    def test_clean_module_set_has_no_cross_module_errors(self):
        """Round 6 replaced the dir-walking helper with the shared discovery path, so the
        collision check now sees exactly the modules the framework would load."""
        from agento.framework.module_discovery import module_dirs_for_validation
        from agento.framework.module_validator import _collision_errors

        candidates = module_dirs_for_validation(
            Path("src/agento/modules"), Path("app/code"),
        )
        assert _collision_errors(candidates) == []


def _module_config_schema(module_dir):
    """Read the fixture module's system.json (empty when it has none)."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(module_dir) / "system.json"
    if not p.is_file():
        return {}
    try:
        data = _json.loads(p.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
