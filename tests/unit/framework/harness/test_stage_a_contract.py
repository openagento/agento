"""Stage A — the harness contract additions that adding a third harness needs.

Three capabilities the contract lacked, all harness-agnostic:

* ``CommandBuilder.stdin_payload`` — a command is argv *plus* stdin, because a CLI
  may accept its prompt only on stdin (an argv prompt starting with ``-`` parses as
  a flag). Both spawn paths must deliver it or they disagree.
* ``capabilities.resume`` enforcement — it was declarative metadata nothing read.
* ``harness_config`` — a harness's own allow-listed config, reachable when the
  command is built, without dragging every module's decrypted secrets along.

The fixture ``fake_harness`` is load-bearing here: its module is ``fake_harness``
while its harness id is ``fake``, so any code deriving a config namespace from the
harness id resolves nothing. The shipped harnesses all happen to have module == id,
so they would hide that bug.
"""
from __future__ import annotations

import ast
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agento.framework.harness import (
    HarnessRunContext,
    RunRequest,
    clear,
    get_harness,
    get_harness_config,
    parse_harness_declarations,
    register_harness,
)
from agento.framework.harness.registry import ObscureRuntimeConfigError
from agento.framework.module_loader import import_class

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "modules"
FAKE = FIXTURES / "fake_harness"


def _string_constants(path: Path) -> set[str]:
    """Every string literal in ``path`` that is a VALUE, excluding docstrings.

    A bare string expression statement — a module/class/function docstring, or a "comment
    string" anywhere in a body — cannot affect behaviour, so it is skipped. Comments never
    reach the AST at all. What remains is the set of strings the code actually uses, which
    is the only place a harness id could steer a framework branch.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    inert = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in inert
    }


def _runner(timeout: int = 5):
    """A minimal concrete SubprocessRunner — the base class is abstract."""
    from agento.framework.harness import RunResult
    from agento.framework.harness.subprocess_runner import SubprocessRunner

    class _R(SubprocessRunner):
        def _parse_output(self, raw: str) -> RunResult:
            return RunResult(raw_output=raw)

        def _credential_env(self, credential):
            return {}

    r = _R.__new__(_R)
    r.context = HarnessRunContext(harness="x", provider="y", timeout_seconds=timeout)
    r.logger = MagicMock()
    r.pid_callback = None
    r.session_id_callback = None
    return r


def _schema(module_dir: Path) -> dict:
    p = module_dir / "system.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def _register_fake(*, fields=None, schema=None):
    clear()
    decl = parse_harness_declarations(FAKE / "di.json", "fake_harness")[0]
    adapter = import_class(FAKE, decl.class_path)()
    register_harness(
        decl.descriptor,
        adapter,
        decl.module,
        decl.runtime_config_fields if fields is None else fields,
        _schema(FAKE) if schema is None else schema,
    )
    return decl


def _register_pi():
    """Register the shipped `pi` harness through the production loader."""
    from agento.framework.bootstrap import _load_agent_harnesses
    from agento.framework.module_loader import scan_modules

    root = Path(__file__).resolve().parents[3].parent / "src" / "agento" / "modules"
    clear()
    _load_agent_harnesses({m.name: m for m in scan_modules(str(root))}["pi"])


class _Svc:
    """Minimal ScopedConfigService stand-in that records what was asked for."""

    def __init__(self, values: dict[str, str]):
        self._values = values
        self.asked: list[str] = []

    def get(self, path: str):
        self.asked.append(path)
        return self._values.get(path)

    def resolve_all(self):  # pragma: no cover - must never be called
        raise AssertionError(
            "get_harness_config must not use resolve_all(): it decrypts every module's "
            "obscure values, and this dict is used to build argv"
        )


class TestStdinPayload:
    def test_shipped_harnesses_keep_stdin_closed(self):
        """claude/codex take the prompt on argv, so stdin stays DEVNULL."""
        from agento.modules.claude.src.command_builder import ClaudeCommandBuilder
        from agento.modules.codex.src.command_builder import CodexCommandBuilder

        ctx = HarnessRunContext(harness="x", provider="y")
        req = RunRequest(prompt="hello")
        assert ClaudeCommandBuilder().stdin_payload(ctx, req) is None
        assert CodexCommandBuilder().stdin_payload(ctx, req) is None

    def test_devnull_when_no_payload_and_pipe_when_payload(self, monkeypatch):
        seen = {}

        class _Proc:
            stdin = None
            stdout = iter(())
            stderr = iter(())
            returncode = 0

            def wait(self, timeout=None):
                return 0

        def _popen(cmd, **kw):
            seen["stdin"] = kw["stdin"]
            p = _Proc()
            p.stdout = iter(())
            p.stderr = iter(())
            p.stdin = MagicMock()
            return p

        monkeypatch.setattr(subprocess, "Popen", _popen)
        runner = _runner()

        runner._execute_process(["c"], {}, None)
        assert seen["stdin"] is subprocess.DEVNULL

        runner._execute_process(["c"], {}, "a prompt")
        assert seen["stdin"] is subprocess.PIPE

    def test_writer_survives_a_child_that_died_before_reading(self, monkeypatch):
        """A failed extension load exits before touching stdin; that must not mask rc."""
        class _Stdin:
            def write(self, _):
                raise BrokenPipeError("child gone")

            def close(self):
                pass

        class _Proc:
            returncode = 1

            def __init__(self):
                self.stdout = iter(())
                self.stderr = iter(())
                self.stdin = _Stdin()

            def wait(self, timeout=None):
                return 1

        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: _Proc())
        runner = _runner()

        proc = runner._execute_process(["c"], {}, "payload")
        assert proc.returncode == 1


class TestHarnessConfigChannel:
    def test_namespace_comes_from_the_declaring_module_not_the_harness_id(self):
        """`fake_harness` module vs `fake` harness id — the whole point of the fixture."""
        _register_fake()
        entry = get_harness("fake")
        assert entry.module == "fake_harness"
        assert entry.descriptor.id == "fake"

        svc = _Svc({"fake_harness/verbose": "1"})
        assert get_harness_config(svc, entry) == {"verbose": "1"}
        assert "fake_harness/verbose" in svc.asked
        assert not any(p.startswith("fake/") for p in svc.asked)

    def test_only_allowlisted_fields_are_resolved(self):
        _register_fake()
        entry = get_harness("fake")
        svc = _Svc(
            {
                "fake_harness/verbose": "1",
                "fake_harness/api_secret": "SHOULD-NEVER-APPEAR",
            }
        )
        cfg = get_harness_config(svc, entry)
        assert cfg == {"verbose": "1"}
        assert "fake_harness/api_secret" not in svc.asked

    def test_obscure_field_in_the_allowlist_is_refused_at_registration(self):
        with pytest.raises(ObscureRuntimeConfigError, match="obscure"):
            _register_fake(fields=("api_secret",))

    def test_unknown_field_in_the_allowlist_is_refused_at_registration(self):
        with pytest.raises(ObscureRuntimeConfigError, match="does not declare"):
            _register_fake(fields=("nope",))

    def test_config_reaches_the_command_builder_as_a_constant_flag(self):
        _register_fake()
        entry = get_harness("fake")
        ctx = HarnessRunContext(
            harness="fake", provider="fake_local", harness_config={"verbose": "1"}
        )
        cmd = entry.adapter.command_builder.headless(ctx, RunRequest(prompt="p"))
        assert "--verbose" in cmd

    def test_config_values_are_never_interpolated_into_argv(self):
        """A config value maps to a fixed flag; it is never pasted into an argument.

        The value is operator-supplied and this builds a command line, so a hostile
        or careless value must not be able to add arguments.
        """
        _register_fake()
        builder = get_harness("fake").adapter.command_builder
        base = builder.headless(
            HarnessRunContext(harness="fake", provider="fake_local"),
            RunRequest(prompt="p"),
        )
        for hostile in ("0; rm -rf /", "--unsafe", "1 --unsafe", "$(whoami)", "hi"):
            cmd = builder.headless(
                HarnessRunContext(
                    harness="fake",
                    provider="fake_local",
                    harness_config={"verbose": hostile},
                ),
                RunRequest(prompt="p"),
            )
            assert cmd == base, f"{hostile!r} added arguments: {cmd}"

    def test_stdin_channel_is_driven_by_that_config(self):
        _register_fake()
        builder = get_harness("fake").adapter.command_builder
        req = RunRequest(prompt="the prompt")
        off = HarnessRunContext(harness="fake", provider="fake_local")
        on = HarnessRunContext(
            harness="fake", provider="fake_local", harness_config={"stdin_prompt": "1"}
        )
        assert builder.stdin_payload(off, req) is None
        assert builder.stdin_payload(on, req) == "the prompt"


class TestStaticManifestValidation:
    def test_obscure_field_is_refused_by_module_validate(self):
        from agento.framework.module_validator import _validate_runtime_config_fields

        errors = _validate_runtime_config_fields(
            {"runtime_config_fields": ["api_secret"]}, 0, FAKE
        )
        assert any("obscure" in e for e in errors)

    def test_unknown_field_is_refused_by_module_validate(self):
        from agento.framework.module_validator import _validate_runtime_config_fields

        errors = _validate_runtime_config_fields(
            {"runtime_config_fields": ["nope"]}, 0, FAKE
        )
        assert any("does not declare" in e for e in errors)

    def test_wellformed_allowlist_passes(self):
        from agento.framework.module_validator import _validate_runtime_config_fields

        assert _validate_runtime_config_fields(
            {"runtime_config_fields": ["verbose", "stdin_prompt"]}, 0, FAKE
        ) == []

    def test_duplicates_are_refused(self):
        from agento.framework.module_validator import _validate_runtime_config_fields

        errors = _validate_runtime_config_fields(
            {"runtime_config_fields": ["verbose", "verbose"]}, 0, FAKE
        )
        assert any("duplicate" in e.lower() for e in errors)


class TestResumeCapabilityIsEnforced:
    def test_fixture_declares_resume_false(self):
        """The gate is only meaningful if something actually declares it off."""
        _register_fake()
        assert get_harness("fake").descriptor.capabilities.resume is False

    def test_shipped_harnesses_declare_resume_true(self):
        from tests.harness_fixtures import register_builtin_harnesses

        register_builtin_harnesses()
        assert get_harness("claude").descriptor.capabilities.resume is True
        assert get_harness("codex").descriptor.capabilities.resume is True


class TestRealBootstrapPath:
    """The registry must be populated correctly by the PRODUCTION path.

    Hand-constructing `RegisteredHarness` (or hand-feeding the schema, as the
    helper above does) passes even when `bootstrap.py` forgets to pass the data —
    which is exactly how the missing `module`/allow-list wiring survived earlier.
    These assertions go through `scan_modules` + the real registration call.
    """

    def test_manifest_config_is_the_system_json_schema_not_defaults(self):
        """`m.config` must be field -> schema-object; the obscure guard depends on it."""
        from agento.framework.module_loader import scan_modules

        manifests = {m.name: m for m in scan_modules(str(FIXTURES))}
        cfg = manifests["fake_harness"].config
        assert isinstance(cfg.get("api_secret"), dict), (
            "manifest.config must carry schema objects; if it ever degrades to "
            "{field: 'default'} strings the obscure guard cannot fire"
        )
        assert cfg["api_secret"]["type"] == "obscure"

    def test_the_real_loader_passes_module_and_allowlist(self):
        """Goes through `_load_agent_harnesses`, the function bootstrap() calls.

        Calling `register_harness` by hand here would pass even if the loader forgot
        an argument — which is precisely the bug this is meant to catch.
        """
        from agento.framework.bootstrap import _load_agent_harnesses
        from agento.framework.module_loader import scan_modules

        clear()
        manifest = {m.name: m for m in scan_modules(str(FIXTURES))}["fake_harness"]
        _load_agent_harnesses(manifest)

        entry = get_harness("fake")
        assert entry.module == "fake_harness"
        assert entry.runtime_config_fields == ("stdin_prompt", "verbose")
        # And the schema really reached the guard: the loader must have handed over
        # system.json, or an obscure field could not have been detected at all.
        svc = _Svc({"fake_harness/verbose": "1"})
        assert get_harness_config(svc, entry) == {"verbose": "1"}

    def test_the_real_loader_refuses_an_obscure_allowlist(self, tmp_path, monkeypatch):
        """An obscure field must fail through the loader, not only via a direct call."""
        import shutil

        from agento.framework.bootstrap import _load_agent_harnesses
        from agento.framework.module_loader import scan_modules

        mod = tmp_path / "fake_harness"
        shutil.copytree(FAKE, mod)
        di = json.loads((mod / "di.json").read_text())
        di["agent_harnesses"][0]["runtime_config_fields"] = ["api_secret"]
        (mod / "di.json").write_text(json.dumps(di))

        clear()
        manifest = {m.name: m for m in scan_modules(str(tmp_path))}["fake_harness"]
        with pytest.raises(ObscureRuntimeConfigError, match="obscure"):
            _load_agent_harnesses(manifest)

    def test_duplicates_are_refused_at_registration_too(self):
        """Both layers, per the plan — a direct caller bypasses the manifest parser."""
        with pytest.raises(ObscureRuntimeConfigError, match="duplicate"):
            _register_fake(fields=("verbose", "verbose"))

    def test_a_non_dict_schema_entry_fails_closed(self):
        """A legacy {field: 'default'} manifest must be refused, not silently allowed."""
        with pytest.raises(ObscureRuntimeConfigError, match="not a schema object"):
            _register_fake(fields=("verbose",), schema={"verbose": "a-default"})


class TestRunCommandDeliversStdin:
    """Exercises `RunCommand.execute` itself, not a lookalike call.

    Round 6's finding was that `input=<str>` without `text=True` raises TypeError
    before the agent is ever spawned — and an argv-asserting mock cannot see it.
    But calling `subprocess.run(['cat'], ...)` directly in a test cannot see it
    either: deleting `input=`/`text=True` from `cli/run.py` would leave such a test
    green. So these drive the real command and let the doubled `subprocess.run`
    delegate to a real `cat`, so the shipped kwargs are the ones under test.
    """

    def _run(self, monkeypatch, runtime: dict):
        from agento.framework.cli import run as run_mod

        captured: dict = {}
        real_run = subprocess.run  # bind before patching, or the double recurses

        def _fake_run(argv, **kwargs):
            captured["kwargs"] = kwargs
            # Delegate to a REAL process with exactly the kwargs cli/run.py passed.
            # A wrong stream mode raises here, just as it would in production.
            proc = real_run(["cat"], capture_output=True, **kwargs)
            captured["stdout"] = proc.stdout
            return proc

        monkeypatch.setattr(run_mod, "find_project_root", lambda: Path("/tmp"))
        monkeypatch.setattr(run_mod, "compose_file_flags", lambda root: ["-f", "x.yml"])
        monkeypatch.setattr(run_mod, "_fetch_runtime", lambda *a, **k: runtime)
        monkeypatch.setattr(run_mod, "_host_build_exists", lambda *a, **k: True)
        monkeypatch.setattr(run_mod.subprocess, "run", _fake_run)

        args = SimpleNamespace(agent_view_code="dev", prompt=["hello"], yolo=False)
        with pytest.raises(SystemExit) as exc:
            run_mod.RunCommand().execute(args)
        captured["exit"] = exc.value.code
        return captured

    def _runtime(self, **over):
        base = {
            "harness": "fake",
            "command": ["fake", "run"],
            "home": "/home/agent",
            "working_dir": "/workspace",
            "env": {},
            "stdin": None,
        }
        base.update(over)
        return base

    def test_payload_is_delivered_to_the_process(self, monkeypatch):
        cap = self._run(monkeypatch, self._runtime(stdin="hello-from-stdin"))
        assert cap["kwargs"].get("input") == "hello-from-stdin"
        assert cap["kwargs"].get("text") is True
        # Proof it survived a real process rather than only being passed as a kwarg.
        assert cap["stdout"] == "hello-from-stdin"
        assert cap["exit"] == 0

    def test_no_payload_keeps_stdin_closed_not_inherited(self, monkeypatch):
        cap = self._run(monkeypatch, self._runtime(stdin=None))
        assert cap["kwargs"].get("stdin") is subprocess.DEVNULL
        assert "input" not in cap["kwargs"], (
            "input=None does NOT mean DEVNULL — it inherits the caller's stdin"
        )
        # bytes, not str: this branch passes no `text=True`, which is itself proof
        # the two branches are genuinely distinct rather than sharing one call shape.
        assert cap["stdout"] == b""


class TestResumeGateBehaviour:
    """`capabilities.resume` must CHANGE BEHAVIOUR, not just be declared.

    The consumer resumes with `RunRequest(prompt="", ...)`. A harness that opens
    the session but does not continue work would exit rc=0 having done nothing and
    be recorded as a success — the worst failure mode available. So a harness that
    declares `resume: false` must start fresh instead.
    """

    def _kwargs(self, **over):
        base = dict(attempt=2, session_id="sess-1", pid_alive=False, can_resume=True)
        base.update(over)
        return base

    def test_resumes_when_the_harness_supports_it(self):
        from agento.framework.consumer import _should_resume

        assert _should_resume(**self._kwargs()) is True

    def test_starts_fresh_when_the_harness_cannot_resume(self):
        from agento.framework.consumer import _should_resume

        assert _should_resume(**self._kwargs(can_resume=False)) is False

    def test_first_attempt_never_resumes(self):
        from agento.framework.consumer import _should_resume

        assert _should_resume(**self._kwargs(attempt=1)) is False

    def test_no_session_id_never_resumes(self):
        from agento.framework.consumer import _should_resume

        assert _should_resume(**self._kwargs(session_id=None)) is False

    def test_live_process_never_resumes(self):
        from agento.framework.consumer import _should_resume

        assert _should_resume(**self._kwargs(pid_alive=True)) is False

    def test_the_gate_matches_what_the_fixture_declares(self):
        """End-to-end on the declaration: fake declares resume:false -> fresh."""
        from agento.framework.consumer import _should_resume

        _register_fake()
        caps = get_harness("fake").descriptor.capabilities
        assert _should_resume(**self._kwargs(can_resume=caps.resume)) is False


class TestProductionWiring:
    """Drive the SHIPPED call paths, not the endpoints they connect.

    Round 2's finding: testing `stdin_payload()` and `_execute_process()` separately
    leaves the line that JOINS them untested, and the same for `_should_resume` and
    `get_harness_config` inside the consumer. Deleting the wiring would keep those
    tests green. Each test below is paired with a mutation proving it fails if the
    wiring line is removed.
    """

    def test_runner_execute_carries_the_builders_stdin_to_the_process(self, monkeypatch):
        """A1: SubprocessRunner.execute() -> builder.stdin_payload -> Popen(stdin=PIPE)."""
        from agento.framework.harness import RunResult
        from agento.framework.harness.subprocess_runner import SubprocessRunner

        seen = {}

        class _Builder:
            def headless(self, ctx, req):
                return ["fake", "run"]

            def interactive(self, ctx, *, yolo=False):
                return ["fake", "shell"]

            def stdin_payload(self, ctx, req):
                return f"PROMPT:{req.prompt}"

        class _R(SubprocessRunner):
            def _parse_output(self, raw: str) -> RunResult:
                return RunResult(raw_output=raw)

            def _credential_env(self, credential):
                return {}

        class _Proc:
            returncode = 0

            def __init__(self):
                self.stdout = iter(())
                self.stderr = iter(())
                self.stdin = MagicMock()

            def wait(self, timeout=None):
                return 0

        def _popen(cmd, **kw):
            seen["stdin_mode"] = kw["stdin"]
            p = _Proc()
            seen["pipe"] = p.stdin
            return p

        monkeypatch.setattr(subprocess, "Popen", _popen)
        ctx = HarnessRunContext(
            harness="fake", provider="fake_local", timeout_seconds=5,
            credential_required=False,   # fake_local declares credential_required: false
        )
        runner = _R(context=ctx, command_builder=_Builder(), logger=MagicMock())
        monkeypatch.setattr(runner, "_record_usage", MagicMock(), raising=False)
        runner.execute(RunRequest(prompt="hello"))

        assert seen["stdin_mode"] is subprocess.PIPE
        seen["pipe"].write.assert_called_once_with("PROMPT:hello")

    def _consumer_run_job(self, monkeypatch, *, attempt, session_id, resume_capable):
        """Drive the real `Consumer._run_job`, capturing the ctx handed to create_runner."""
        from agento.framework import consumer as cons

        _register_fake()
        entry = get_harness("fake")
        if not resume_capable:
            assert entry.descriptor.capabilities.resume is False

        captured = {}

        class _Runner:
            def observe(self, **kw):
                pass

            def execute(self, request):
                captured["request"] = request
                from agento.framework.harness import RunResult

                return RunResult(raw_output="ok")

        def _create_runner(harness, ctx, **kw):
            captured["ctx"] = ctx
            return _Runner()

        class _Svc:
            def get(self, path):
                return {"fake_harness/verbose": "1"}.get(path)

            def get_module(self, name):
                return {}

        runtime = SimpleNamespace(
            harness="fake", provider="fake_local", model="m",
            workspace=SimpleNamespace(id=1, code="ws"),
            agent_view=SimpleNamespace(id=7, code="dev"),
        )
        monkeypatch.setattr(cons, "get_channel", lambda src: MagicMock())
        monkeypatch.setattr(cons, "get_connection", lambda cfg: MagicMock())
        monkeypatch.setattr(cons, "resolve_agent_view_runtime", lambda c, av: runtime)
        monkeypatch.setattr(cons, "materialize_run_workspace", lambda *a, **k: (None, None))
        monkeypatch.setattr(cons, "create_runner", _create_runner)
        monkeypatch.setattr(cons, "get_module_config", lambda src: {})
        monkeypatch.setattr(
            "agento.framework.config_resolver.ScopedConfigService",
            lambda *a, **k: _Svc(),
        )

        workflow = MagicMock()
        workflow.return_value.execute_job.return_value = SimpleNamespace(
            raw_output="ok", harness="fake", provider="fake_local", model="m",
            input_tokens=0, output_tokens=0, prompt="p", session_id=None,
            mcp_init=None, stats_line="", raw="",
        )
        monkeypatch.setattr(cons, "get_workflow_class", lambda t: workflow)

        c = cons.Consumer.__new__(cons.Consumer)
        c.logger = MagicMock()
        c.model_override = None
        c._db_config = MagicMock()
        c._consumer_config = SimpleNamespace(job_timeout_seconds=60, disable_llm=False)
        c._credential_resolver = MagicMock()
        # The lease bookkeeping the credential lifecycle touches on every exit path.
        c._active_jobs_lock = threading.Lock()
        c._held_leases = {}
        c._save_pid = lambda *a: None
        c._save_session_id = lambda *a: None
        c._is_pid_alive = lambda pid: False

        job = SimpleNamespace(
            id=1, agent_view_id=7, source="blank", type="blank", attempt=attempt,
            session_id=session_id, pid=None, priority=0, reference_id=None,
        )
        c._run_job(job)
        return captured

    def test_consumer_puts_allowlisted_config_on_the_context(self, monkeypatch):
        """A3: consumer.py must call get_harness_config and attach it to the ctx."""
        cap = self._consumer_run_job(
            monkeypatch, attempt=1, session_id=None, resume_capable=False
        )
        assert cap["ctx"].harness_config == {"verbose": "1"}

    def test_consumer_starts_fresh_when_the_harness_cannot_resume(self, monkeypatch):
        """A2: with a saved session on attempt 2, `resume:false` must NOT resume.

        Hardcoding `can_resume=True` at the consumer's call site would make this fail:
        the request would carry the session id and an empty prompt.
        """
        cap = self._consumer_run_job(
            monkeypatch, attempt=2, session_id="sess-1", resume_capable=False
        )
        assert "request" not in cap or cap["request"].session_id != "sess-1", (
            "fake declares resume:false, so the consumer must not take the resume branch"
        )


class TestShippedHarnessesLoadThroughBootstrap:
    """The plan requires every shipped harness to register via the PRODUCTION loader and
    the framework to name none of them.

    A previous round added `pi` to `tests/harness_fixtures.py` and claimed that tuple
    "drives the framework-source guard". It does not — the guard is a separate test that
    only covered the fixture harness. Claiming coverage that does not exist is worse than
    the gap, so both halves are asserted here directly.
    """

    def _load(self, module: str):
        from agento.framework.bootstrap import _load_agent_harnesses
        from agento.framework.module_loader import scan_modules

        root = Path(__file__).resolve().parents[3].parent / "src" / "agento" / "modules"
        manifest = {m.name: m for m in scan_modules(str(root))}[module]
        _load_agent_harnesses(manifest)
        return manifest

    @pytest.mark.parametrize("module", ["claude", "codex", "pi"])
    def test_registers_through_the_real_loader(self, module):
        from tests.harness_fixtures import BUILTIN_HARNESS_MODULES

        assert module in BUILTIN_HARNESS_MODULES, (
            f"{module!r} ships but is missing from BUILTIN_HARNESS_MODULES, so the shared "
            f"fixtures do not cover it"
        )
        clear()
        self._load(module)
        # The harness id is not required to equal the module name, so discover it.
        from agento.framework.harness import list_harnesses

        entries = list(list_harnesses())
        assert entries, (
            f"module {module!r} registered no harness through _load_agent_harnesses"
        )
        entry = entries[0]
        assert entry.module == module
        assert entry.adapter is not None
        # And it is reachable by the id the descriptor declares, which need not equal the
        # module name.
        assert get_harness(entry.descriptor.id) is entry

    def test_pi_config_channel_survives_the_real_loader(self):
        """`pi` is the first shipped harness to use `runtime_config_fields`, so the
        allow-list must arrive through the production path, not just a hand-built call."""
        clear()
        self._load("pi")
        entry = get_harness("pi")
        assert entry.module == "pi"
        assert "builtin_tools" in entry.runtime_config_fields
        assert "allow_model_substitution" in entry.runtime_config_fields
        svc = _Svc({"pi/builtin_tools": "0", "pi/allow_model_substitution": "1"})
        assert get_harness_config(svc, entry) == {
            "builtin_tools": "0",
            "allow_model_substitution": "1",
        }

    @pytest.mark.parametrize("harness_id", ["pi", "claude", "codex"])
    def test_no_framework_source_file_names_the_harness(self, harness_id):
        """The framework must never branch on a harness id — the whole point of the contract.

        Asserted over the **AST**, not the file text. A text search for `'"pi"'` only
        recognised double quotes, so `'pi'` walked straight through the gate; it also fired
        on prose, which is why it had to be pinned to one quote style in the first place.
        Comparing parsed string CONSTANTS makes quote style irrelevant and lets docstrings
        and comments discuss a harness freely — they cannot influence behaviour.
        """
        framework = Path(__file__).resolve().parents[3].parent / "src" / "agento" / "framework"
        offenders = {
            path.relative_to(framework).as_posix()
            for path in framework.rglob("*.py")
            if harness_id in _string_constants(path)
        }
        assert offenders == set(), (
            f"framework source uses the literal {harness_id!r} as a value: "
            f"{sorted(offenders)}. Harness ids belong in module manifests, never in "
            f"framework branches."
        )


class TestPrepareWorkspaceConfigIsBackwardCompatible:
    """`prepare_workspace` gained `harness_config` after third-party adapters were already
    possible.

    A default in the Protocol does NOT make someone else's implementation accept an unknown
    keyword — passing it unconditionally raises `TypeError` in their adapter. So callers ask
    first, via `supply_harness_config`.
    """

    def test_an_old_signature_adapter_does_not_receive_the_keyword(self):
        from agento.framework.harness import supply_harness_config

        class LegacyAdapter:
            def prepare_workspace(self, working_dir, agent_config, *, agent_view_id=None, toolbox_url):
                pass

        base = {"agent_view_id": 1, "toolbox_url": "http://tb:3001"}
        out = supply_harness_config(LegacyAdapter(), base, {"builtin_tools": "0"})
        assert "harness_config" not in out
        # And calling with the result must not raise.
        LegacyAdapter().prepare_workspace(Path("/tmp"), {}, **out)

    def test_a_new_signature_adapter_does_receive_it(self):
        from agento.framework.harness import supply_harness_config

        class ModernAdapter:
            def prepare_workspace(
                self, working_dir, agent_config, *, agent_view_id=None, toolbox_url,
                harness_config=None,
            ):
                pass

        out = supply_harness_config(
            ModernAdapter(),
            {"agent_view_id": 1, "toolbox_url": "http://tb:3001"},
            {"builtin_tools": "0"},
        )
        assert out["harness_config"] == {"builtin_tools": "0"}

    def test_a_kwargs_adapter_receives_it(self):
        from agento.framework.harness import supply_harness_config

        class KwargsAdapter:
            def prepare_workspace(self, working_dir, agent_config, **kwargs):
                pass

        out = supply_harness_config(KwargsAdapter(), {}, {"builtin_tools": "0"})
        assert "harness_config" in out

    def test_an_empty_config_is_never_passed(self):
        """A harness declaring no runtime_config_fields must see no change at all."""
        from agento.framework.harness import supply_harness_config

        class ModernAdapter:
            def prepare_workspace(
                self, working_dir, agent_config, *, toolbox_url, harness_config=None
            ):
                pass

        out = supply_harness_config(ModernAdapter(), {"toolbox_url": "x"}, {})
        assert "harness_config" not in out

    def test_the_shipped_adapters_all_accept_it(self):
        """claude/codex declare no runtime_config_fields, but they must not break when a
        caller supplies the keyword."""
        import inspect as _inspect

        from agento.modules.claude.src.config import ClaudeWorkspaceAdapter
        from agento.modules.codex.src.config import CodexWorkspaceAdapter
        from agento.modules.pi.src.config import PiWorkspaceAdapter

        for cls in (ClaudeWorkspaceAdapter, CodexWorkspaceAdapter, PiWorkspaceAdapter):
            params = _inspect.signature(cls.prepare_workspace).parameters
            assert "harness_config" in params, f"{cls.__name__} rejects harness_config"


class TestPerRunModelReachesInjectionThroughTheRealCopyPath:
    """The join, not the endpoints.

    `PiWorkspaceAdapter.inject_runtime_params` accepted `effective_model` while nothing
    supplied it, so a build carrying `expected_model: build/time` stayed stale after being
    copied for a job — and a legitimate `--model` override would be failed by its own
    guard. A direct adapter test cannot catch that; this drives
    `copy_build_to_artifacts_dir`, the shared path both the consumer and the host use.
    """

    def _build(self, root: Path) -> Path:
        from agento.modules.pi.src.config import PiWorkspaceAdapter

        build = root / "build"
        build.mkdir()
        PiWorkspaceAdapter().prepare_workspace(
            build,
            {"provider": "openrouter", "model": "build/time"},
            agent_view_id=7,
            toolbox_url="http://tb:3001",
        )
        return build

    def _conn(self, run_dir: Path) -> dict:
        return json.loads((run_dir / ".pi" / "agento-toolbox.json").read_text())

    def test_the_effective_model_replaces_the_stale_build_value(self, tmp_path):
        from agento.framework.artifacts_dir import copy_build_to_artifacts_dir

        _register_pi()
        build = self._build(tmp_path)
        run = tmp_path / "run"
        copy_build_to_artifacts_dir(
            build, run, job_id=42, harness="pi",
            effective_model="run/time", effective_provider="openrouter",
        )
        payload = self._conn(run)
        assert payload["expected_model"] == "run/time", (
            "the per-run override never reached injection — the defect this guards"
        )
        assert payload["url"].endswith("job_id=42")

    def test_without_an_override_the_build_value_survives(self, tmp_path):
        from agento.framework.artifacts_dir import copy_build_to_artifacts_dir

        _register_pi()
        build = self._build(tmp_path)
        run = tmp_path / "run"
        copy_build_to_artifacts_dir(build, run, job_id=43, harness="pi")
        assert self._conn(run)["expected_model"] == "build/time"

    def test_injection_must_not_undo_the_router_opt_out(self, tmp_path):
        """A per-run injection must not re-enable the model check for a router.

        Originally the opt-out WAS the absence of `expected_model`, so this asserted the
        key stayed absent. A review then reproduced what that conflation costs: a build
        with no model configured looks identical to a router opt-out, so an explicit
        `--model` run got no guard either. The opt-out is now an explicit marker, and what
        must survive injection is the MARKER — refreshing the expectation beside it is
        both harmless and required by the other three spawn paths.
        """
        from agento.framework.artifacts_dir import copy_build_to_artifacts_dir
        from agento.modules.pi.src.config import PiWorkspaceAdapter

        _register_pi()
        build = tmp_path / "build"
        build.mkdir()
        PiWorkspaceAdapter().prepare_workspace(
            build,
            {"provider": "openrouter", "model": "openrouter/free"},
            agent_view_id=7,
            toolbox_url="http://tb:3001",
            harness_config={"allow_model_substitution": "1"},
        )
        assert self._conn(build)["allow_model_substitution"] is True

        run = tmp_path / "run"
        copy_build_to_artifacts_dir(
            build, run, job_id=44, harness="pi",
            effective_model="poolside/laguna-xs-2.1:free", effective_provider="openrouter",
        )
        payload = self._conn(run)
        assert payload["allow_model_substitution"] is True, (
            "runtime injection undid the router opt-out"
        )
        assert payload["expected_provider"] == "openrouter"
