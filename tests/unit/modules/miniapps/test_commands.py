import argparse
import json
import subprocess

import pytest

from agento.modules.miniapps.src.commands.activate import MiniappActivateCommand
from agento.modules.miniapps.src.commands.deactivate import MiniappDeactivateCommand
from agento.modules.miniapps.src.commands.list import MiniappListCommand

V1 = "v-20260101-120000-abcd"


@pytest.fixture
def toolbox(monkeypatch, tmp_path):
    monkeypatch.setattr("agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr("agento.framework.cli._project.compose_file_flags",
                        lambda _root: ["-f", "docker-compose.yml"], raising=False)
    calls, replies = [], []

    def run(cmd, **kw):
        calls.append((cmd, json.loads(kw["input"])))
        body, code = replies.pop(0)
        return subprocess.CompletedProcess(cmd, code, stdout=json.dumps(body) + "\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return calls, replies


def _parse(cmd, argv):
    parser = argparse.ArgumentParser()
    cmd.configure(parser)
    return parser.parse_args(argv)


def test_activate_sends_the_actions_to_the_miniapps_cli(toolbox, capsys):
    calls, replies = toolbox
    replies.append(({"allowed_actions": ["jira_search"]}, 0))
    cmd = MiniappActivateCommand()
    cmd.execute(_parse(cmd, ["site", V1, "--actions", "jira_search, "]))
    argv, payload = calls[0]
    assert "/app/modules/core/miniapps/toolbox/cli.js" in argv and argv[argv.index("--op") + 1] == "activate"
    assert payload == {"artifact_code": "site", "version_id": V1, "actions": ["jira_search"]}
    assert "jira_search" in capsys.readouterr().out


def test_activate_without_actions_asks_for_all(toolbox):
    calls, replies = toolbox
    replies.append(({"allowed_actions": []}, 0))
    cmd = MiniappActivateCommand()
    cmd.execute(_parse(cmd, ["site", V1]))
    assert calls[0][1]["actions"] is None


def test_a_refusal_prints_the_error_and_exits_1(toolbox, capsys):
    _calls, replies = toolbox
    replies.append(({"error_code": "MANIFEST_INVALID", "message": "the version has no valid miniapp.json"}, 1))
    cmd = MiniappActivateCommand()
    with pytest.raises(SystemExit) as exc:
        cmd.execute(_parse(cmd, ["site", V1]))
    assert exc.value.code == 1
    assert "MANIFEST_INVALID" in capsys.readouterr().err


def test_deactivate_and_list(toolbox, capsys):
    calls, replies = toolbox
    replies.append(({"activated": False}, 0))
    replies.append(({"activations": [{"artifact_code": "site", "version_id": V1, "allowed_actions": ["a"]}]}, 0))
    d = MiniappDeactivateCommand()
    d.execute(_parse(d, ["site", V1]))
    lst = MiniappListCommand()
    lst.execute(_parse(lst, []))
    assert [c[0][c[0].index("--op") + 1] for c in calls] == ["deactivate", "list"]
    out = capsys.readouterr().out
    assert "deactivated" in out and f"site  {V1}  actions: a" in out
