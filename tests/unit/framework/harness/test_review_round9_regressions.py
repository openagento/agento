"""Regression tests for impl review round 9.

Theme: every one of these is a case where my fix was correct for the two SHIPPED harnesses
and wrong as a contract — an argv shape, a stream, a manifest form, a provider count.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework.harness import (
    DuplicateCredentialScopeError,
    HarnessDescriptor,
    RunRequest,
    clear,
    register_harness,
)
from tests.harness_fixtures import make_runner, register_builtin_harnesses

REPO = Path(__file__).resolve().parents[4]

SECRET = "SECRET-PROMPT-CONTENT-9f1a"


@pytest.fixture(autouse=True)
def _harnesses():
    register_builtin_harnesses()
    yield
    clear()


def _runner():
    r = make_runner("claude", credential=None, credential_required=False)
    r._record_usage = MagicMock()
    return r


class TestArgvIsNeverLogged:
    """Round 9 fixed redaction to cover ``--prompt=<content>``; round 10 showed the whole
    approach was unsound — a third-party builder can TRANSFORM the prompt past any substring
    match (JSON-escaping a newline) and can carry secrets of its own (``--api-key=…``) that
    the framework cannot enumerate. So no argv-derived string is logged at all; only an
    allowlist of metadata. These tests keep the earlier attack strings as regression inputs.
    """

    def test_metadata_contains_no_argv_content(self):
        runner = _runner()
        secret_arg = "--api-key=sk-review-secret"
        meta = runner._cmd_metadata(
            ["agent", secret_arg, f"--prompt={SECRET}"], RunRequest(prompt=SECRET),
        )

        assert SECRET not in meta
        assert "sk-review-secret" not in meta
        assert "--api-key" not in meta

    def test_a_transformed_prompt_cannot_leak(self):
        """The case substring redaction could never have caught."""
        runner = _runner()
        prompt = "line one\nline two"
        escaped = json.dumps(prompt)  # what a JSON-emitting builder would pass

        meta = runner._cmd_metadata(["agent", f"--p={escaped}"], RunRequest(prompt=prompt))

        assert "line one" not in meta and "line two" not in meta

    def test_metadata_keeps_the_useful_facts(self):
        runner = _runner()
        meta = runner._cmd_metadata(
            ["claude", "-p", SECRET, "--model", "opus"],
            RunRequest(prompt=SECRET, model="opus"),
        )

        assert "bin=claude" in meta
        assert "argv=5" in meta
        assert f"prompt_len={len(SECRET)}" in meta
        assert "model=set" in meta
        assert "resume=no" in meta

    def test_resume_is_reported(self):
        runner = _runner()
        meta = runner._cmd_metadata(
            ["claude", "--resume", "s1"], RunRequest(prompt="", session_id="s1"),
        )
        assert "resume=yes" in meta
        assert "prompt_len=0" in meta

    def test_executable_is_the_only_argv_element_reported(self):
        """`cmd[0]` is chosen by the harness module, never by a prompt or credential."""
        runner = _runner()
        meta = runner._cmd_metadata(["mybin", "secret-a", "secret-b"], RunRequest(prompt="x"))

        assert "bin=mybin" in meta
        assert "secret-a" not in meta and "secret-b" not in meta

    def test_the_redaction_helper_is_gone(self):
        """Keeping it would invite a caller to log argv "safely" again."""
        assert not hasattr(_runner(), "_redacted_cmd")


class TestStderrOnlyFailuresKeepTheirOutput:
    """``agent_output = stdout`` persisted an EMPTY string for a harness that writes its
    diagnostics to stderr — leaving the operator with no record at all, which is the exact
    failure mode this path exists to prevent."""

    def test_helper_falls_back_to_stderr(self):
        assert _runner()._failure_output("", "boom on stderr") == "boom on stderr"

    def test_helper_keeps_stdout_alone_when_stderr_is_empty(self):
        assert _runner()._failure_output("out", "") == "out"

    def test_helper_combines_both_with_a_marker(self):
        combined = _runner()._failure_output("out", "err")
        assert "out" in combined
        assert "--- stderr ---" in combined and "err" in combined

    def test_helper_is_empty_only_when_both_are(self):
        assert _runner()._failure_output("", "") == ""

    def test_real_timeout_path_attaches_stderr_only_output(self, monkeypatch):
        """Drives the ACTUAL timeout branch through a fake Popen, rather than injecting
        `agent_output` — which is what the round-8 test did, and why it could not catch this.
        """
        runner = _runner()

        class _FakeProc:
            pid = 4242
            returncode = None
            # Real iterables: `_execute_process` drains both streams in threads.
            stdout = iter(())
            stderr = iter(["diagnostic on stderr\n"])

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 1)

            def kill(self):
                return None

        monkeypatch.setattr(
            "agento.framework.harness.subprocess_runner.subprocess.Popen",
            lambda *a, **kw: _FakeProc(),
        )

        with pytest.raises(subprocess.TimeoutExpired) as exc:
            runner.execute(RunRequest(prompt="p"))

        # stdout was empty, so `agent_output = stdout` would have persisted "".
        attached = getattr(exc.value, "agent_output", None)
        assert attached, "stderr-only timeout persisted an empty output"
        assert "diagnostic on stderr" in attached

    def test_rc_failure_also_uses_the_helper(self):
        runner = _runner()
        runner._execute_process = MagicMock(
            return_value=MagicMock(returncode=1, stdout="", stderr="only-stderr"),
        )
        with pytest.raises(RuntimeError) as exc:
            runner.execute(RunRequest(prompt="p"))

        assert "only-stderr" in getattr(exc.value, "agent_output", "")


class TestLegacySandboxPackagesTriggerRebuild:
    """`manifest.py` promises the deprecated top-level ``sandbox_packages`` array for one
    more release, so a module that has not migrated must still trigger the sandbox rebuild —
    otherwise its CLI is absent after enable, or left installed after disable."""

    def _legacy_module(self, root: Path) -> Path:
        m = root / "app" / "code" / "legacy_harness"
        m.mkdir(parents=True)
        (m / "module.json").write_text(json.dumps({"name": "legacy_harness", "version": "1"}))
        (m / "di.json").write_text(json.dumps({"sandbox_packages": [{
            "provider": "legacy_harness", "manager": "npm", "package": "@x/legacy",
            "binary": "legacy", "version_env_key": "LEGACY_VERSION",
            "default_range": "1.0.0",
        }]}))
        return root

    def test_legacy_declaration_yields_a_pin(self, tmp_path):
        from agento.framework.cli.module import _declared_sandbox_pins

        root = self._legacy_module(tmp_path)
        assert _declared_sandbox_pins(root, "legacy_harness") == {"LEGACY_VERSION"}

    def test_modern_declaration_still_yields_its_pin(self):
        from agento.framework.cli.module import _declared_sandbox_pins

        assert _declared_sandbox_pins(REPO, "codex") == {"CODEX_VERSION"}

    def test_both_forms_in_one_module_are_merged(self, tmp_path):
        from agento.framework.cli.module import _declared_sandbox_pins

        m = tmp_path / "app" / "code" / "both"
        m.mkdir(parents=True)
        (m / "module.json").write_text(json.dumps({"name": "both", "version": "1"}))
        (m / "di.json").write_text(json.dumps({
            "agent_harnesses": [{
                "id": "both", "label": "B", "class": "src.a.A",
                "default_provider": "p",
                "providers": [{"id": "p", "credential_required": False}],
                "sandbox_package": {
                    "manager": "npm", "package": "@x/modern", "binary": "modern",
                    "version_env_key": "MODERN_VERSION", "default_range": "1.0.0",
                },
            }],
            "sandbox_packages": [{
                "provider": "both", "manager": "npm", "package": "@x/old",
                "binary": "old", "version_env_key": "OLD_VERSION",
                "default_range": "1.0.0",
            }],
        }))

        assert _declared_sandbox_pins(tmp_path, "both") == {
            "MODERN_VERSION", "OLD_VERSION",
        }

    def test_a_malformed_legacy_entry_does_not_crash_the_toggle(self, tmp_path):
        """module:validate reports it; the toggle must not blow up."""
        from agento.framework.cli.module import _declared_sandbox_pins

        m = tmp_path / "app" / "code" / "broken"
        m.mkdir(parents=True)
        (m / "module.json").write_text(json.dumps({"name": "broken", "version": "1"}))
        (m / "di.json").write_text(json.dumps({"sandbox_packages": [{"provider": "x"}]}))

        assert _declared_sandbox_pins(tmp_path, "broken") == set()


class TestScopeUniquenessWithinOneHarness:
    """The owner check compared against OTHER harnesses only, and the authenticator check
    collapsed keys through `set(scopes)` — so two providers of the SAME harness could claim
    one scope, breaking the one-scope-one-pool invariant just as thoroughly."""

    def _adapter(self, scopes):
        class _A:
            command_builder = workspace_adapter = transcript_reader = None
            authenticators = dict.fromkeys(scopes, object())

            def create_runner(self, ctx, **kwargs):  # pragma: no cover
                return None

        return _A()

    def _descriptor(self, scope_a: str, scope_b: str) -> HarnessDescriptor:
        return HarnessDescriptor.from_declaration({
            "id": "dup", "label": "D", "default_provider": "a",
            "providers": [
                {"id": "a", "credential_required": True, "credential_scope": scope_a,
                 "registration_modes": ["api_key"]},
                {"id": "b", "credential_required": True, "credential_scope": scope_b,
                 "registration_modes": ["api_key"]},
            ],
        })

    def test_two_providers_sharing_a_scope_are_rejected(self):
        clear()
        descriptor = self._descriptor("shared", "shared")

        with pytest.raises(DuplicateCredentialScopeError, match="more than"):
            register_harness(descriptor, self._adapter(["shared"]))

    def test_the_error_names_the_duplicated_scope(self):
        clear()
        with pytest.raises(DuplicateCredentialScopeError, match="shared"):
            register_harness(self._descriptor("shared", "shared"), self._adapter(["shared"]))

    def test_distinct_scopes_on_one_harness_are_fine(self):
        clear()
        descriptor = self._descriptor("scope_a", "scope_b")

        register_harness(descriptor, self._adapter(["scope_a", "scope_b"]))

        from agento.framework.harness import list_credential_scopes

        assert list_credential_scopes() == ["scope_a", "scope_b"]

    def test_registry_is_unchanged_after_a_rejected_registration(self):
        """A partial registration would leave a scope pointing at a harness that is not in
        the registry."""
        clear()
        register_builtin_harnesses()
        before = dict(_scope_owners())

        with pytest.raises(DuplicateCredentialScopeError):
            register_harness(self._descriptor("shared", "shared"), self._adapter(["shared"]))

        assert _scope_owners() == before


def _scope_owners() -> dict:
    from agento.framework.harness import registry

    return dict(registry._SCOPE_OWNERS)


class TestAdminDocsMatchTheRealBindings:
    def test_documented_keys_exist_on_the_screen(self):
        from agento.framework.admin.screens.credentials import CredentialsScreen

        bound = {b.key for b in CredentialsScreen.BINDINGS}
        doc = (REPO / "docs" / "cli" / "admin.md").read_text()
        start = doc.index("### Credentials") + len("### Credentials")
        credentials_section = doc[start:]
        credentials_section = credentials_section[: credentials_section.index("### ")]

        # The doc claimed an `s` "set primary" action the screen never bound.
        assert "`s`" not in credentials_section
        assert "`r`" in credentials_section and "r" in bound
        assert "`x`" in credentials_section and "x" in bound

    def test_identity_doc_binds_both_config_axes(self):
        text = (REPO / "docs" / "config" / "identity.md").read_text()

        assert "agent_view/harness" in text
        assert "agent_view/provider anthropic" in text
