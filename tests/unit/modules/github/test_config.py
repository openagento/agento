import json
import pathlib

from agento.modules.github.src.config import GitHubConfig


def test_enabled_parses_string_falsy_values():
    for raw in (False, 0, "0", "false", "False", None):
        assert GitHubConfig.from_dict({"enabled": raw}).enabled is False
    for raw in (True, 1, "1", "true"):
        assert GitHubConfig.from_dict({"enabled": raw}).enabled is True


def test_poll_top_defaults_and_clamps():
    assert GitHubConfig.from_dict({}).poll_top == 20
    assert GitHubConfig.from_dict({"poll_top": "garbage"}).poll_top == 20
    assert GitHubConfig.from_dict({"poll_top": None}).poll_top == 20
    assert GitHubConfig.from_dict({"poll_top": 0}).poll_top == 1
    assert GitHubConfig.from_dict({"poll_top": 999}).poll_top == 100


def test_repo_list_splits_trims_and_dedupes_preserving_order():
    cfg = GitHubConfig.from_dict({"repo_allowlist": " api , web ,api, "})
    assert cfg.repo_list == ["api", "web"]


def test_config_has_no_token_field():
    assert "token" not in {f for f in GitHubConfig.__dataclass_fields__}


def test_token_and_identity_fields_are_agent_view_only_in_system_json():
    """The framework enforces showIn* flags in `config:set` (framework/cli/config.py ->
    config_schema.is_scope_allowed), so these flags are the ENFORCED half of the
    "token never at DEFAULT scope" invariant that bitbucket documents but only asserts in prose."""
    root = pathlib.Path(__file__).resolve().parents[4]
    system = json.loads((root / "src/agento/modules/github/system.json").read_text())
    for field in ("github_token", "github_login", "repo_allowlist"):
        assert system[field]["showInDefault"] is False, field
        assert system[field]["showInWorkspace"] is False, field
        assert system[field].get("showInAgentView", True) is True, field
