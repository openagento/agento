"""`job:set-runtime`: a test job stays a test job (D-TEST-RUN-1)."""
from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.cli.runtime import SetRuntimeCommand
from agento.framework.test_run import TEST_MODE_ENV, InvalidTestConfig


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    SetRuntimeCommand().configure(parser)
    return parser.parse_args(argv)


def _execute(argv: list[str], **set_runtime):
    with patch("agento.framework.cli.runtime._load_framework_config",
               return_value=(MagicMock(), MagicMock(), MagicMock())), \
            patch("agento.framework.cli.runtime.get_connection_or_exit", return_value=MagicMock()), \
            patch("agento.framework.job_store.set_job_runtime", **set_runtime) as set_job_runtime:
        SetRuntimeCommand().execute(_parse(argv))
    return set_job_runtime


def test_set_runtime_has_no_no_test_flag():
    with pytest.raises(SystemExit):
        _parse(["7", "--no-test"])


def test_set_runtime_refuses_edit_of_job_with_invalid_policy(capsys):
    with pytest.raises(SystemExit) as exit_:
        _execute(["7", "--model", "x"],
                 side_effect=InvalidTestConfig("job 7 has an invalid test policy; replace it with --test"))
    assert exit_.value.code == 1
    assert capsys.readouterr().err.startswith("Error: job 7 has an invalid test policy")


def test_set_runtime_test_stores_legacy_shape(monkeypatch):
    monkeypatch.setenv(TEST_MODE_ENV, "1")
    job = MagicMock(id=7, agent_type=None, model=None, test_config={"stub": [], "real": []})
    set_job_runtime = _execute(["7", "--test", "{}"], return_value=job)
    assert set_job_runtime.call_args.kwargs["test_config"] == {"stub": [], "real": []}
