"""Tests for the shared pre-spawn pipeline used by the consumer and by
the ``agento run`` path: freshness check, artifacts dir, and build copy.

Mirrors the inline block in ``consumer._run_job`` (lines 453-486) so the
extraction is behavior-preserving.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus
from agento.framework.events import WorkspaceBuildCheckEvent
from agento.modules.claude.src.config import ClaudeWorkspaceAdapter
from tests.harness_fixtures import stub_workspace_adapters

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


@dataclass
class _AV:
    code: str
    id: int


@dataclass
class _WS:
    code: str
    id: int


@dataclass
class _Runtime:
    agent_view: _AV | None
    workspace: _WS | None
    harness: str | None


def _claude_token(token_id: int, type_: str, label: str, credentials: dict) -> CredentialRecord:
    now = datetime(2026, 1, 1, 0, 0)
    return CredentialRecord(
        id=token_id,
        scope="claude",
        type=type_,
        label=label,
        credentials=credentials,
        token_limit=0,
        enabled=True,
        status=CredentialStatus.OK,
        priority=0,
        error_msg=None,
        expires_at=None,
        used_at=None,
        created_at=now,
        updated_at=now,
    )


def _claude_oauth_token(token_id: int, label: str, access_token: str, email: str) -> CredentialRecord:
    return _claude_token(token_id, "oauth", label, {
        "subscription_key": access_token,
        "refresh_token": f"refresh-{token_id}",
        "raw_auth": {
            "credentials": {
                "claudeAiOauth": {
                    "accessToken": access_token,
                    "refreshToken": f"refresh-{token_id}",
                },
            },
            "claude_json": {
                "oauthAccount": {"emailAddress": email},
                "userID": f"user-{token_id}",
            },
        },
    })


class TestMaterializeRunWorkspace:
    def test_returns_none_when_no_agent_view(self):
        from agento.framework.run_preparation import materialize_run_workspace
        runtime = _Runtime(agent_view=None, workspace=None, harness="claude")
        em = MagicMock()
        home, working = materialize_run_workspace(runtime, run_id=1, em=em)
        assert home is None and working is None
        em.dispatch.assert_not_called()

    def test_dispatches_freshness_check(self, tmp_path):
        from agento.framework.run_preparation import materialize_run_workspace
        runtime = _Runtime(
            agent_view=_AV(code="dev", id=7),
            workspace=_WS(code="acme", id=3),
            harness=None,  # no harness → no copy, but freshness still dispatched
        )
        em = MagicMock()
        with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path / "build")):
            materialize_run_workspace(runtime, run_id=1, em=em)

        assert em.dispatch.called
        name, payload = em.dispatch.call_args.args
        assert name == "workspace_build_check_before"
        assert isinstance(payload, WorkspaceBuildCheckEvent)
        assert payload.agent_view_id == 7

    @pytest.mark.parametrize("thread_key", [None, "0123456789abcdef0123456789abcdef"],
                             ids=["agento-run", "job"])
    def test_a_job_run_links_its_threads_shared_dir(self, tmp_path, thread_key):
        import os

        from agento.framework.run_preparation import materialize_run_workspace
        from agento.framework.runner import uids
        runtime = _Runtime(agent_view=_AV(code="dev", id=7), workspace=_WS(code="acme", id=3),
                           harness=None)
        with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path / "build")), \
             patch.object(uids, "THREAD_GID", os.getgid()):
            _home, run = materialize_run_workspace(runtime, run_id=1, em=MagicMock(),
                                                   thread_key=thread_key)
        link = run / "shared"
        if thread_key is None:
            assert not link.exists() and not (tmp_path / "artifacts" / "_threads").exists()
        else:
            assert os.readlink(link) == str(tmp_path / "artifacts" / "_threads" / thread_key / "shared")

    def test_reraises_event_error(self, tmp_path):
        from agento.framework.run_preparation import materialize_run_workspace
        runtime = _Runtime(
            agent_view=_AV(code="dev", id=7),
            workspace=_WS(code="acme", id=3),
            harness="claude",
        )

        class _Boom(RuntimeError):
            pass

        def _set_error(_name, event):
            event.error = _Boom("rebuild failed")

        em = MagicMock()
        em.dispatch.side_effect = _set_error

        with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path / "build")), \
             pytest.raises(_Boom, match="rebuild failed"):
            materialize_run_workspace(runtime, run_id=1, em=em)

    def test_returns_paths_and_copies_build(self, tmp_path):
        from agento.framework.run_preparation import materialize_run_workspace
        # Stage a "current" build dir the function can resolve.
        ws, av = "acme", "dev"
        build_root = tmp_path / "build"
        actual_build = build_root / ws / av / "20250101-000000"
        actual_build.mkdir(parents=True)
        (actual_build / "AGENTS.md").write_text("hello")
        current_link = build_root / ws / av / "current"
        current_link.symlink_to(actual_build)

        runtime = _Runtime(
            agent_view=_AV(code=av, id=7),
            workspace=_WS(code=ws, id=3),
            harness=None,  # skip ConfigWriter injection
        )
        em = MagicMock()

        with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path / "build")):
            home, working = materialize_run_workspace(runtime, run_id="run", em=em)

        assert home == tmp_path / "artifacts" / ws / av / "run"
        assert working == tmp_path / "artifacts" / ws / av / "run"
        assert working.is_dir()
        # Universal file should have been copied (not symlinked)
        assert (working / "AGENTS.md").is_file()
        assert not (working / "AGENTS.md").is_symlink()

    def test_writes_selected_token_credentials_to_artifacts_home_only(self, tmp_path):
        from agento.framework.run_preparation import materialize_run_workspace

        ws, av = "acme", "dev"
        build_root = tmp_path / "build"
        actual_build = build_root / ws / av / "builds" / "1"
        actual_build.mkdir(parents=True)
        (actual_build / "AGENTS.md").write_text("hello")
        (actual_build / ".agent" / "sessions").mkdir(parents=True)
        (actual_build / ".agent" / "sessions" / "build-session.jsonl").write_text("old")
        current_link = build_root / ws / av / "current"
        current_link.symlink_to(actual_build)

        token = object()
        writer = MagicMock()
        writer.owned_paths.return_value = (set(), {".agent"})
        writer.persistent_home_paths.return_value = [".agent/sessions"]

        def write_credentials(home_dir, selected_token):
            assert selected_token is token
            (home_dir / ".selected-token").write_text("run-token")

        writer.write_credentials.side_effect = write_credentials

        runtime = _Runtime(
            agent_view=_AV(code=av, id=7),
            workspace=_WS(code=ws, id=3),
            harness="claude",
        )

        with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(build_root)), \
             stub_workspace_adapters(claude=writer):
            home, working = materialize_run_workspace(
                runtime, run_id=99, em=MagicMock(), credential=token,
            )

        assert home == working == tmp_path / "artifacts" / ws / av / "99"
        assert (home / ".selected-token").read_text() == "run-token"
        assert not (actual_build / ".selected-token").exists()
        # No link into state/ (the runner gives a run uid its run dir only), and no copy of
        # the store: the run resumes no session, so it gets none.
        sessions = home / ".agent" / "sessions"
        assert not sessions.exists() and not sessions.is_symlink()
        assert (build_root / ws / av / "state" / ".agent" / "sessions").is_dir()
        assert (actual_build / ".agent" / "sessions").is_dir()
        assert not (actual_build / ".agent" / "sessions").is_symlink()
        writer.write_credentials.assert_called_once()
        assert writer.write_credentials.call_args.args[0] == home

    def test_mixed_claude_tokens_are_isolated_per_run_home(self, tmp_path):
        from agento.framework.run_preparation import materialize_run_workspace

        ws, av = "acme", "dev"
        build_root = tmp_path / "build"
        actual_build = build_root / ws / av / "builds" / "1"
        actual_build.mkdir(parents=True)
        (actual_build / "AGENTS.md").write_text("hello")
        claude_dir = actual_build / ".claude"
        (claude_dir / "backups").mkdir(parents=True)
        (claude_dir / ".credentials.json").write_text("stale-oauth")
        backup_path = claude_dir / "backups" / ".claude.json.backup.1780206566242"
        backup_path.write_text("stale")
        (actual_build / ".claude.json").write_text(json.dumps({
            "model": "sonnet",
            "oauthAccount": {"emailAddress": "stale@example.com"},
            "userID": "stale-user",
        }))
        current_link = build_root / ws / av / "current"
        current_link.symlink_to(actual_build)

        oauth_a = _claude_oauth_token(1, "oauth-a", "oauth-token-a", "a@example.com")
        oauth_b = _claude_oauth_token(2, "oauth-b", "oauth-token-b", "b@example.com")
        api_key = _claude_token(
            3, "anthropic_api_key", "api-key", {"api_key": "sk-ant-api"},
        )
        selected = [
            oauth_a, oauth_b, api_key, oauth_a, oauth_b,
            api_key, oauth_a, oauth_b, api_key, oauth_a,
        ]
        writer = ClaudeWorkspaceAdapter()
        runtime = _Runtime(
            agent_view=_AV(code=av, id=7),
            workspace=_WS(code=ws, id=3),
            harness="claude",
        )

        with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(build_root)), \
             stub_workspace_adapters(claude=writer):
            homes = [
                materialize_run_workspace(
                    runtime,
                    run_id=i,
                    em=MagicMock(),
                    credential=token,
                )[0]
                for i, token in enumerate(selected, start=1)
            ]

        assert len(set(homes)) == 10
        for i, (home, token) in enumerate(zip(homes, selected, strict=True), start=1):
            assert home is not None
            assert home == tmp_path / "artifacts" / ws / av / str(i)
            assert home.is_dir()
            assert not (home / ".claude" / "projects").exists()
            assert not (home / ".claude" / "todos").is_symlink()
            claude_json = json.loads((home / ".claude.json").read_text())
            assert claude_json["model"] == "sonnet"
            if token.type == "anthropic_api_key":
                assert not (home / ".claude" / ".credentials.json").exists()
                assert "oauthAccount" not in claude_json
                assert "userID" not in claude_json
                assert not (
                    home / ".claude" / "backups" / ".claude.json.backup.1780206566242"
                ).exists()
                assert writer.credential_env(token) == {"ANTHROPIC_API_KEY": "sk-ant-api"}
            else:
                data = json.loads((home / ".claude" / ".credentials.json").read_text())
                assert data["claudeAiOauth"]["accessToken"] == (
                    token.credentials["subscription_key"]
                )
                selected_email = token.credentials["raw_auth"]["claude_json"][
                    "oauthAccount"
                ]["emailAddress"]
                assert claude_json["oauthAccount"]["emailAddress"] == (
                    selected_email
                )

        assert (actual_build / ".claude" / ".credentials.json").read_text() == "stale-oauth"
        build_json = json.loads((actual_build / ".claude.json").read_text())
        assert build_json["oauthAccount"]["emailAddress"] == "stale@example.com"
        assert backup_path.exists()


def _codex_model(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("model ="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


# (harness, relative output path, how to read the model out of that file)
_SIBLING_CASES = [
    ("claude", ".claude.json", lambda text: json.loads(text).get("model")),
    ("codex", ".codex/config.toml", _codex_model),
]


class TestTheNoBuildFallbackOverlayReachesEverySibling:
    """The overlay is generic, so assert it on the REAL sibling adapters.

    `run_preparation` overlays the per-run effective model into the `agent_config` the
    no-build fallback materialises from. `claude/src/config.py:384` and
    `codex/src/config.py:299` both read `agent_config["model"]`, so a `--model` override now
    reaches their generated config on that path — a behaviour change to two harnesses this
    work was not aimed at.

    A first version of these tests substituted a `_Writer` double for BOTH adapters and
    asserted what the double received. A reviewer pointed out that proves nothing about the
    siblings: mutate either real adapter to stop reading `agent_config["model"]` and those
    tests still pass. So these drive the real adapters and assert the model that lands in
    the file each one actually writes.
    """

    def _materialize(self, tmp_path, harness, effective_model):
        from unittest.mock import MagicMock, patch

        from agento.framework.run_preparation import materialize_run_workspace

        class _Svc:
            def resolve_all(self):
                return {"agent_view/model": "configured-model"}

            def get(self, path):
                return None

        runtime = _Runtime(
            agent_view=_AV(code="dev", id=7),
            workspace=_WS(code="acme", id=3),
            harness=harness,
        )
        kwargs = {} if effective_model is None else {"effective_model": effective_model}
        with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "a")), \
             patch("agento.framework.artifacts_dir.BUILD_DIR", str(tmp_path / "b")), \
             patch("agento.framework.run_preparation.BUILD_DIR", str(tmp_path / "b")), \
             patch("agento.framework.harness.persistent_home_paths_for", return_value=[]):
            _, working = materialize_run_workspace(
                runtime,
                run_id="cli-1",
                agent_config_svc=_Svc(),
                em=MagicMock(),
                **kwargs,
            )
        return working

    @pytest.mark.parametrize("harness,rel,extract", _SIBLING_CASES)
    def test_the_override_lands_in_the_real_adapters_output(
        self, tmp_path, harness, rel, extract,
    ):
        working = self._materialize(tmp_path, harness, "override-model")
        written = working / rel
        assert written.is_file(), f"{harness} wrote no {rel}"
        assert extract(written.read_text()) == "override-model"

    @pytest.mark.parametrize("harness,rel,extract", _SIBLING_CASES)
    def test_without_an_override_the_configured_model_lands_instead(
        self, tmp_path, harness, rel, extract,
    ):
        working = self._materialize(tmp_path, harness, None)
        written = working / rel
        assert written.is_file(), f"{harness} wrote no {rel}"
        assert extract(written.read_text()) == "configured-model"
# ---------------------------------------------------------------------------
# SSH identity: the non-secret files only, and the private key never a file
# ---------------------------------------------------------------------------
_PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQy\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)

_SSH_VALUES = {
    "agent_view/identity/ssh_private_key": _PEM,
    "agent_view/identity/ssh_public_key": "ssh-ed25519 AAAA host",
    "agent_view/identity/ssh_config": "Host github.com\n",
    "agent_view/identity/ssh_known_hosts": "github.com ssh-ed25519 AAAA\n",
}


class _CountingSvc:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return self.values.get(path)


def _staged_build(tmp_path, *, ssh_payload=None, ssh_symlink_to=None):
    """A `current` build the pipeline can copy, optionally carrying legacy SSH state."""
    ws, av = "acme", "dev"
    build_root = tmp_path / "build"
    actual_build = build_root / ws / av / "builds" / "1"
    actual_build.mkdir(parents=True)
    (actual_build / "AGENTS.md").write_text("hello")
    if ssh_symlink_to is not None:
        ssh_symlink_to.mkdir(parents=True, exist_ok=True)
        (actual_build / ".ssh").symlink_to(ssh_symlink_to)
    elif ssh_payload is not None:
        (actual_build / ".ssh").mkdir()
        (actual_build / ".ssh" / "id_rsa").write_text(ssh_payload)
    (build_root / ws / av / "current").symlink_to(actual_build)
    return ws, av, build_root, actual_build


def _materialize(tmp_path, build_root, ws, av, **kwargs):
    from agento.framework.run_preparation import materialize_run_workspace

    runtime = _Runtime(
        agent_view=_AV(code=av, id=7), workspace=_WS(code=ws, id=3), harness=None,
    )
    with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
         patch("agento.framework.artifacts_dir.BUILD_DIR", str(build_root)):
        return materialize_run_workspace(runtime, run_id="run", em=MagicMock(), **kwargs)


class TestRunSshIdentity:
    def test_no_file_under_the_run_dir_contains_private_key_material(self, tmp_path):
        """AC4, the core guard."""
        ws, av, build_root, _ = _staged_build(tmp_path)
        svc = _CountingSvc(_SSH_VALUES)

        home, _ = _materialize(tmp_path, build_root, ws, av, agent_config_svc=svc)

        for path in home.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assert "PRIVATE KEY" not in path.read_text(errors="ignore"), path
        assert not (home / ".ssh" / "id_rsa").exists()
        # ... while the non-secret files ARE there.
        assert (home / ".ssh" / "id_rsa.pub").read_text() == "ssh-ed25519 AAAA host"
        assert (home / ".ssh" / "config").read_text() == "Host github.com\n"
        assert (home / ".ssh" / "known_hosts").read_text() == "github.com ssh-ed25519 AAAA\n"

    def test_returns_the_unchanged_two_tuple(self, tmp_path):
        """The signature gained a keyword-only param; the RETURN contract did not change.

        The existing call-site tests in this file and in
        tests/unit/framework/harness/test_review_round8_regressions.py are therefore
        untouched — if a future revision does change the contract, those are the files
        that must change with it.
        """
        ws, av, build_root, _ = _staged_build(tmp_path)
        result = _materialize(
            tmp_path, build_root, ws, av, agent_config_svc=_CountingSvc(_SSH_VALUES),
        )
        assert isinstance(result, tuple) and len(result) == 2
        assert result[0] == result[1]

    def test_a_legacy_key_the_build_carried_is_removed(self, tmp_path):
        ws, av, build_root, _ = _staged_build(tmp_path, ssh_payload=_PEM)
        home, _ = _materialize(
            tmp_path, build_root, ws, av, agent_config_svc=_CountingSvc(_SSH_VALUES),
        )
        assert not (home / ".ssh" / "id_rsa").exists()

    def test_a_failed_scrub_aborts_the_run(self, tmp_path):
        """Fail closed: starting with a readable id_rsa in the run HOME reproduces the bug."""
        ws, av, build_root, _ = _staged_build(tmp_path, ssh_payload=_PEM)
        from pathlib import Path as _P
        with patch.object(_P, "unlink", side_effect=PermissionError("denied")), \
             pytest.raises(PermissionError):
            _materialize(
                tmp_path, build_root, ws, av, agent_config_svc=_CountingSvc(_SSH_VALUES),
            )

    def test_a_symlinked_ssh_dir_from_a_prefix_build_becomes_a_real_dir(self, tmp_path):
        """The symlink trap: writing through it would land in the SHARED build dir."""
        shared = tmp_path / "shared_build_ssh"
        ws, av, build_root, _ = _staged_build(tmp_path, ssh_symlink_to=shared)
        home, _ = _materialize(
            tmp_path, build_root, ws, av, agent_config_svc=_CountingSvc(_SSH_VALUES),
        )
        assert (home / ".ssh").is_dir()
        assert not (home / ".ssh").is_symlink()
        assert not (shared / "id_rsa.pub").exists()
        assert (home / ".ssh" / "id_rsa.pub").is_file()

    def test_none_config_service_warns_and_does_not_raise(self, tmp_path, caplog):
        import logging

        ws, av, build_root, _ = _staged_build(tmp_path)
        with caplog.at_level(logging.WARNING):
            home, _ = _materialize(tmp_path, build_root, ws, av, agent_config_svc=None)
        assert home is not None
        assert "no SSH identity" in caplog.text

    def test_a_legacy_key_is_removed_even_with_no_config_service(self, tmp_path, caplog):
        """The scrub is a precondition of the RUN, not a step of identity materialization.

        A run with no config service gets no identity — that says nothing about whether it
        may read a key an older build left behind, so the scrub must not sit behind the
        service. This is the shape the PROD incident used.
        """
        import logging

        ws, av, build_root, _ = _staged_build(tmp_path, ssh_payload=_PEM)
        with caplog.at_level(logging.WARNING):
            home, _ = _materialize(tmp_path, build_root, ws, av, agent_config_svc=None)
        assert not (home / ".ssh" / "id_rsa").exists()
        assert "no SSH identity" in caplog.text

    def test_a_failed_scrub_aborts_the_run_with_no_config_service_too(self, tmp_path):
        ws, av, build_root, _ = _staged_build(tmp_path, ssh_payload=_PEM)
        from pathlib import Path as _P
        with patch.object(_P, "unlink", side_effect=PermissionError("denied")), \
             pytest.raises(PermissionError):
            _materialize(tmp_path, build_root, ws, av, agent_config_svc=None)

    def test_a_symlinked_ssh_dir_is_replaced_with_no_config_service_too(self, tmp_path):
        """Writing through the link would reach the SHARED build dir — identity or not."""
        shared = tmp_path / "shared_build_ssh"
        ws, av, build_root, _ = _staged_build(tmp_path, ssh_symlink_to=shared)
        (shared / "id_rsa").write_text(_PEM)
        home, _ = _materialize(tmp_path, build_root, ws, av, agent_config_svc=None)
        assert (home / ".ssh").is_dir() and not (home / ".ssh").is_symlink()
        assert not (home / ".ssh" / "id_rsa").exists()

    def test_an_already_resolved_identity_is_not_re_read(self, tmp_path):
        """One resolution per run: the spawn path resolves, then passes the value in."""
        from agento.framework.ssh_identity import ResolvedSshIdentity

        ws, av, build_root, _ = _staged_build(tmp_path)
        svc = _CountingSvc(_SSH_VALUES)
        home, _ = _materialize(
            tmp_path, build_root, ws, av,
            agent_config_svc=svc,
            ssh_identity=ResolvedSshIdentity(public_key="from-caller"),
        )
        assert svc.calls == []
        assert (home / ".ssh" / "id_rsa.pub").read_text() == "from-caller"


@pytest.mark.usefixtures("builtin_harnesses")
def test_a_job_pinned_to_another_harness_gets_its_own_files_and_not_the_views(tmp_path):
    """The build is made for the view's harness (claude). A job pinned to pi needs pi's
    own files, and must not see claude's — the claude login lives there."""
    from agento.framework.run_preparation import materialize_run_workspace

    build_root = tmp_path / "build"
    build = build_root / "acme" / "dev" / "builds" / "1"
    (build / ".claude").mkdir(parents=True)
    (build / ".claude" / ".credentials.json").write_text("claude-oauth")
    (build / ".claude.json").write_text("{}")
    (build / "AGENTS.md").write_text("hello")
    (build_root / "acme" / "dev" / "current").symlink_to(build)

    with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
         patch("agento.framework.artifacts_dir.BUILD_DIR", str(build_root)):
        _, run_dir = materialize_run_workspace(
            _Runtime(agent_view=_AV(code="dev", id=7), workspace=_WS(code="acme", id=3),
                     harness="pi"),
            run_id=1, em=MagicMock(),
        )

    assert (run_dir / ".pi" / "agento-toolbox.js").is_file()
    assert (run_dir / "AGENTS.md").exists()
    assert not (run_dir / ".claude").exists()
    assert not (run_dir / ".claude.json").exists()


def _agent_finder(root, sid):
    return max(root.glob(f".agent/sessions/*/{sid}.jsonl"), key=lambda p: p.stat().st_mtime,
               default=None)


@pytest.fixture
def persistent_view(tmp_path):
    """A view whose harness keeps `.agent/sessions` in state/, with three saved sessions."""
    ws, av = "acme", "dev"
    build_root = tmp_path / "build"
    actual_build = build_root / ws / av / "builds" / "1"
    actual_build.mkdir(parents=True)
    (build_root / ws / av / "current").symlink_to(actual_build)
    sessions = build_root / ws / av / "state" / ".agent" / "sessions"
    for name in ("old", "kept", "other"):
        (sessions / "b0").mkdir(parents=True, exist_ok=True)
        (sessions / "b0" / f"{name}.jsonl").write_text(name)
    (sessions / "b0" / "old" / "subagents").mkdir(parents=True)
    (sessions / "b0" / "old" / "subagents" / "a.jsonl").write_text("a")
    writer = MagicMock()
    writer.owned_paths.return_value = (set(), {".agent"})
    writer.persistent_home_paths.return_value = [".agent/sessions"]
    runtime = _Runtime(agent_view=_AV(code=av, id=7), workspace=_WS(code=ws, id=3), harness="claude")
    with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
         patch("agento.framework.artifacts_dir.BUILD_DIR", str(build_root)), \
         stub_workspace_adapters(claude=writer):
        yield runtime, sessions


def _files(root):
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} if root.exists() else set()


class TestPersistentState:
    def _materialize(self, runtime, finder=_agent_finder, **kw):
        from agento.framework.run_preparation import materialize_run_workspace
        with patch("agento.framework.run_preparation.session_finder", return_value=finder):
            return materialize_run_workspace(runtime, run_id=99, em=MagicMock(), **kw)[0]

    def _save(self, runtime, home, finder=_agent_finder, **kw):
        from agento.framework.run_preparation import save_persistent_state
        with patch("agento.framework.run_preparation.session_finder", return_value=finder):
            save_persistent_state(runtime, home, **kw)

    def test_materialize_copies_only_resumed_session(self, persistent_view):
        runtime, _ = persistent_view
        home = self._materialize(runtime, resume_session_ids=("old",))
        assert _files(home / ".agent" / "sessions") == {"b0/old.jsonl", "b0/old/subagents/a.jsonl"}

    def test_materialize_without_resume_has_no_session_store(self, persistent_view):
        runtime, _ = persistent_view
        home = self._materialize(runtime)
        assert not (home / ".agent" / "sessions").exists()

    def test_materialize_adapter_without_session_file_copies_all(self, persistent_view):
        """A harness that predates `session_file` still gets its whole store (CODE-5)."""
        runtime, sessions = persistent_view
        home = self._materialize(runtime, finder=None, resume_session_ids=("old",))
        assert _files(home / ".agent" / "sessions") == _files(sessions)

    def test_round_trip_saves_moved_session_to_its_state_path_and_drops(self, persistent_view, tmp_path):
        """Only what the run changed goes back; a moved session goes back to its own path; an
        untouched file never overwrites a concurrent run's newer write; a link is never
        followed; once saved, the persistent paths leave the run dir."""
        runtime, sessions = persistent_view
        home = self._materialize(runtime, resume_session_ids=("old", "kept"))
        run_sessions = home / ".agent" / "sessions"
        (run_sessions / "b1").mkdir()
        (run_sessions / "b0" / "old.jsonl").rename(run_sessions / "b1" / "old.jsonl")
        (run_sessions / "b0" / "old").rename(run_sessions / "b1" / "old")
        (run_sessions / "b1" / "old.jsonl").write_text("old+turn")
        os.utime(run_sessions / "b1" / "old.jsonl", (2_000_000_000, 2_000_000_000))
        (run_sessions / "b1" / "new.jsonl").write_text("new")
        (sessions / "b0" / "kept.jsonl").write_text("newer write of another run")
        os.utime(sessions / "b0" / "kept.jsonl", (2_000_000_000, 2_000_000_000))
        (tmp_path / "secret").write_text("secret")
        (run_sessions / "leak").symlink_to(tmp_path / "secret")

        self._save(runtime, home, session_ids=("old", "kept", "new"))

        assert (sessions / "b0" / "old.jsonl").read_text() == "old+turn"
        assert not (sessions / "b1" / "old.jsonl").exists()
        assert not (sessions / "b1" / "old").exists()
        assert (sessions / "b1" / "new.jsonl").read_text() == "new"
        assert (sessions / "b0" / "kept.jsonl").read_text() == "newer write of another run"
        assert not (sessions / "leak").exists()
        assert not os.path.lexists(run_sessions)

    def test_save_persistent_state_keeps_paths_on_conflict(self, persistent_view):
        runtime, sessions = persistent_view
        home = self._materialize(runtime)
        (sessions / "b0" / "x.jsonl").mkdir()
        (sessions / "b0" / "x.jsonl" / "odd").write_text("odd")
        (home / ".agent" / "sessions" / "b0").mkdir(parents=True)
        (home / ".agent" / "sessions" / "b0" / "x.jsonl").write_text("x")
        self._save(runtime, home)
        assert (home / ".agent" / "sessions" / "b0" / "x.jsonl").read_text() == "x"

    def test_save_persistent_state_keeps_on_copy_back_error(self, persistent_view):
        runtime, _ = persistent_view
        home = self._materialize(runtime, resume_session_ids=("old",))
        with patch("agento.framework.run_preparation.copy_back_persistent_paths",
                   side_effect=OSError(28, "No space left on device")), \
             pytest.raises(OSError):
            self._save(runtime, home)
        assert (home / ".agent" / "sessions" / "b0" / "old.jsonl").read_text() == "old"


def test_link_state_links_the_persistent_paths_for_an_agent_run(tmp_path):
    """`agento run` runs its CLI as `agent`: its HOME links to state/, so the session persists."""
    from agento.framework.run_preparation import materialize_run_workspace

    build_root = tmp_path / "build"
    actual_build = build_root / "acme" / "dev" / "builds" / "1"
    actual_build.mkdir(parents=True)
    (build_root / "acme" / "dev" / "current").symlink_to(actual_build)
    writer = MagicMock()
    writer.owned_paths.return_value = (set(), {".agent"})
    writer.persistent_home_paths.return_value = [".agent/sessions"]
    runtime = _Runtime(agent_view=_AV(code="dev", id=7), workspace=_WS(code="acme", id=3), harness="claude")

    with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
         patch("agento.framework.artifacts_dir.BUILD_DIR", str(build_root)), \
         stub_workspace_adapters(claude=writer):
        home, _ = materialize_run_workspace(runtime, run_id="run-1", em=MagicMock(), link_state=True)

    assert (home / ".agent" / "sessions").resolve() == (
        build_root / "acme" / "dev" / "state" / ".agent" / "sessions"
    ).resolve()


def test_two_claude_turns_keep_one_session_copy_in_state(tmp_path):
    """Turn 2 resumes turn 1's session in another run dir (another cwd bucket). After it,
    state/ holds exactly one copy of the session and no run dir keeps a persistent path.
    Real claude adapter, `session_file` and `prepare_resume`."""
    from agento.framework.harness import HarnessRunContext
    from agento.framework.run_preparation import materialize_run_workspace, save_persistent_state
    from agento.modules.claude.src.runner import ClaudeSubprocessRunner

    ws, av, sid = "acme", "dev", "a8390dba-a29e-4f18-9498-79b7ccc4bbdc"
    build_root = tmp_path / "build"
    actual_build = build_root / ws / av / "builds" / "1"
    actual_build.mkdir(parents=True)
    (build_root / ws / av / "current").symlink_to(actual_build)
    state = build_root / ws / av / "state"
    runtime = _Runtime(agent_view=_AV(code=av, id=7), workspace=_WS(code=ws, id=3), harness="claude")

    with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(tmp_path / "artifacts")), \
         patch("agento.framework.artifacts_dir.BUILD_DIR", str(build_root)), \
         stub_workspace_adapters(claude=ClaudeWorkspaceAdapter()):
        homes = []
        for turn, resume in ((1, ()), (2, (sid,))):
            home, _ = materialize_run_workspace(runtime, run_id=turn, em=MagicMock(),
                                                resume_session_ids=resume)
            cwd = f"/workspace/artifacts/{ws}/{av}/{turn}"
            ctx = HarnessRunContext(harness="claude", provider="p", working_dir=cwd,
                                    home_dir=str(home))
            if resume:
                assert ClaudeSubprocessRunner(context=ctx, command_builder=None).prepare_resume(sid)
            bucket = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
            bucket.mkdir(parents=True, exist_ok=True)
            with (bucket / f"{sid}.jsonl").open("a") as f:
                f.write(f"turn {turn}\n")
            save_persistent_state(runtime, home, session_ids=(*resume, sid))
            homes.append(home)

    copies = list(state.glob(f".claude/projects/*/{sid}.jsonl"))
    assert len(copies) == 1
    assert copies[0].read_text() == "turn 1\nturn 2\n"
    for home in homes:
        assert not (home / ".claude" / "projects").exists()
        assert not (home / ".claude" / "todos").exists()
