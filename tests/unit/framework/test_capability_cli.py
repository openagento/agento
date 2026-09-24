import argparse
import io

import pytest

from agento.framework.cli import capability as cap_cli
from agento.framework.toolbox_capability import (
    INTERACTIVE_CAPABILITY_TTL_SECONDS,
    KIND_INTERNAL_REST,
    KIND_MCP_INTERACTIVE,
    REST_CAPABILITY_TTL_SECONDS,
)


class _Conn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _View:
    id = 7


_DEFAULT_VIEW = _View()


def _patch_env(monkeypatch, *, view=_DEFAULT_VIEW, issued="tok-abc", revoked=True):
    calls = {"issue": [], "revoke": []}
    monkeypatch.setattr(cap_cli, "_load_framework_config", lambda: ({}, None, None))
    monkeypatch.setattr(cap_cli, "get_connection_or_exit", lambda cfg: _Conn())
    monkeypatch.setattr(
        "agento.framework.workspace.get_agent_view_by_code", lambda conn, code: view
    )

    def _issue(conn, **kw):
        calls["issue"].append(kw)
        return issued

    def _revoke(conn, token):
        calls["revoke"].append(token)
        return revoked

    monkeypatch.setattr(cap_cli, "issue_capability", _issue)
    monkeypatch.setattr(cap_cli, "revoke_capability", _revoke)
    return calls


def _mint_args(**kw):
    base = {"kind": KIND_INTERNAL_REST, "agent_view": "dev", "ttl": None, "transport": "http"}
    base.update(kw)
    return argparse.Namespace(**base)


def _parsed_mint(argv):
    parser = argparse.ArgumentParser()
    cap_cli.CapabilityMintCommand().configure(parser)
    return parser.parse_args(argv)


def test_capability_mint_refuses_mcp_job():
    # mcp_job belongs to a job the consumer runs; a hand-minted one would outlive
    # the terminal transition that revokes it, so it is not even a valid --kind.
    with pytest.raises(SystemExit):
        _parsed_mint(["--kind", "mcp_job", "--agent-view", "dev"])
    ok = _parsed_mint(["--kind", KIND_MCP_INTERACTIVE, "--agent-view", "dev"])
    assert ok.kind == KIND_MCP_INTERACTIVE


@pytest.mark.parametrize(
    "kind,ttl",
    [
        (KIND_INTERNAL_REST, REST_CAPABILITY_TTL_SECONDS + 1),
        (KIND_MCP_INTERACTIVE, INTERACTIVE_CAPABILITY_TTL_SECONDS + 1),
        (KIND_INTERNAL_REST, 0),
        (KIND_INTERNAL_REST, -1),
    ],
)
def test_mint_ttl_is_capped_per_kind(monkeypatch, capsys, kind, ttl):
    calls = _patch_env(monkeypatch)
    with pytest.raises(SystemExit):
        cap_cli.CapabilityMintCommand().execute(_mint_args(kind=kind, ttl=ttl))
    # An out-of-range TTL is an error, never a silent clamp — nothing is issued.
    assert calls["issue"] == []
    assert "must be between" in capsys.readouterr().err


def test_mint_ttl_below_the_maximum_is_accepted(monkeypatch):
    calls = _patch_env(monkeypatch)
    cap_cli.CapabilityMintCommand().execute(_mint_args(ttl=60))
    assert calls["issue"][0]["ttl_seconds"] == 60


def test_mint_defaults_the_ttl_to_the_kinds_maximum(monkeypatch):
    calls = _patch_env(monkeypatch)
    cap_cli.CapabilityMintCommand().execute(_mint_args(kind=KIND_MCP_INTERACTIVE))
    assert calls["issue"][0]["ttl_seconds"] == INTERACTIVE_CAPABILITY_TTL_SECONDS


def test_mint_issues_http_only_by_default_and_names_a_cli_subject(monkeypatch):
    calls = _patch_env(monkeypatch)
    cap_cli.CapabilityMintCommand().execute(_mint_args())
    assert calls["issue"][0]["allowed_transports"] == ["http"]
    assert calls["issue"][0]["subject_id"] == "service:cli"
    assert _parsed_mint(["--kind", KIND_INTERNAL_REST, "--agent-view", "dev"]).transport == "http"


@pytest.mark.parametrize("transport,expected", [("sse", ["sse"]), ("both", ["sse", "http"])])
def test_mint_sse_transport_is_capped_at_the_sse_ttl(monkeypatch, transport, expected):
    calls = _patch_env(monkeypatch)
    cap_cli.CapabilityMintCommand().execute(_mint_args(kind=KIND_MCP_INTERACTIVE, transport=transport))
    assert calls["issue"][0]["allowed_transports"] == expected
    assert calls["issue"][0]["ttl_seconds"] == 14400
    assert calls["issue"][0]["subject_id"] is None
    with pytest.raises(SystemExit):
        cap_cli.CapabilityMintCommand().execute(
            _mint_args(kind=KIND_MCP_INTERACTIVE, transport=transport, ttl=14401)
        )


def test_mint_ttl_rejects_a_non_integer():
    with pytest.raises(SystemExit):
        _parsed_mint(["--kind", KIND_INTERNAL_REST, "--agent-view", "dev", "--ttl", "abc"])


def test_mint_prints_exactly_one_token_and_nothing_else(monkeypatch, capsys):
    _patch_env(monkeypatch, issued="tok-secret")
    cap_cli.CapabilityMintCommand().execute(_mint_args())
    out, err = capsys.readouterr()
    # stdout is the token alone, so TOKEN=$(agento capability:mint …) works.
    assert out == "tok-secret\n"
    assert "tok-secret" not in err


def test_mint_rejects_an_unknown_agent_view(monkeypatch, capsys):
    calls = _patch_env(monkeypatch, view=None)
    with pytest.raises(SystemExit):
        cap_cli.CapabilityMintCommand().execute(_mint_args(agent_view="ghost"))
    assert calls["issue"] == []
    assert "not found" in capsys.readouterr().err


def test_revoke_reads_the_token_from_stdin(monkeypatch, capsys):
    calls = _patch_env(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("tok-abc\n"))
    cap_cli.CapabilityRevokeCommand().execute(argparse.Namespace())
    # There is deliberately no --id and no token argument: argv is world-readable.
    assert calls["revoke"] == ["tok-abc"]
    assert "revoked" in capsys.readouterr().err


def test_revoke_takes_no_token_argument():
    parser = argparse.ArgumentParser()
    cap_cli.CapabilityRevokeCommand().configure(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["tok-abc"])


def test_revoke_fails_when_stdin_is_empty(monkeypatch, capsys):
    calls = _patch_env(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit):
        cap_cli.CapabilityRevokeCommand().execute(argparse.Namespace())
    assert calls["revoke"] == []
    assert "No token" in capsys.readouterr().err


def test_revoke_reports_a_token_that_matches_no_live_capability(monkeypatch, capsys):
    _patch_env(monkeypatch, revoked=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("stale\n"))
    with pytest.raises(SystemExit):
        cap_cli.CapabilityRevokeCommand().execute(argparse.Namespace())
    assert "No live capability" in capsys.readouterr().err
