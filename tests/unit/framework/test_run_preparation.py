"""Tests for the shared pre-spawn pipeline used by the consumer and by
the ``agento run`` path: freshness check, artifacts dir, and build copy.

Mirrors the inline block in ``consumer._run_job`` (lines 453-486) so the
extraction is behavior-preserving.
"""
from __future__ import annotations

import json
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
        session_link = home / ".agent" / "sessions"
        assert session_link.is_symlink()
        assert session_link.resolve() == (
            build_root / ws / av / "state" / ".agent" / "sessions"
        ).resolve()
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
            assert (home / ".claude" / "projects").is_symlink()
            assert (home / ".claude" / "todos").is_symlink()
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
