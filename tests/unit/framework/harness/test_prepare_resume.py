"""A conversation's next turn resumes a session made by an EARLIER job, in another run dir.

claude and pi file sessions under a slug of their cwd and look only there, so the session
must be moved into the new run's folder. The folder names below are the ones seen on a dev
stack (job 26981 claude, job 26977 pi).
"""
from __future__ import annotations

import os

import pytest

from agento.framework.harness import HarnessRunContext
from agento.modules.claude.src.runner import ClaudeSubprocessRunner
from agento.modules.pi.src.runner import PiSubprocessRunner

SID = "a8390dba-a29e-4f18-9498-79b7ccc4bbdc"

CASES = {
    "claude": (ClaudeSubprocessRunner, ".claude/projects",
               "-workspace-artifacts-support-qa-01-26981", f"{SID}.jsonl",
               "/workspace/artifacts/support/qa_01/26983",
               "-workspace-artifacts-support-qa-01-26983"),
    "pi": (PiSubprocessRunner, ".pi/agent/sessions",
           "--workspace-artifacts-it-dev_01-26977--", f"2026-10-06T07-01-32-017Z_{SID}.jsonl",
           "/workspace/artifacts/it/dev_01/26984",
           "--workspace-artifacts-it-dev_01-26984--"),
}


def _runner(harness, home, cwd):
    cls = CASES[harness][0]
    ctx = HarnessRunContext(harness=harness, provider="p", working_dir=cwd, home_dir=str(home))
    return cls(context=ctx, command_builder=None)


@pytest.mark.parametrize("harness", CASES)
def test_a_session_from_an_earlier_run_dir_is_moved_here(tmp_path, harness):
    _, store, old, name, cwd, new = CASES[harness]
    (tmp_path / store / old).mkdir(parents=True)
    (tmp_path / store / old / name).write_text("{}\n")

    assert _runner(harness, tmp_path, cwd).prepare_resume(SID) is True
    assert (tmp_path / store / new / name).read_text() == "{}\n"
    assert not (tmp_path / store / old / name).exists()  # one file per session
    # A second call (a retry of the same job) finds it in place.
    assert _runner(harness, tmp_path, cwd).prepare_resume(SID) is True


@pytest.mark.parametrize("harness", CASES)
def test_the_newest_copy_wins(tmp_path, harness):
    _, store, old, name, cwd, new = CASES[harness]
    for i, folder in enumerate(("a", old)):
        (tmp_path / store / folder).mkdir(parents=True)
        (tmp_path / store / folder / name).write_text(folder)
        os.utime(tmp_path / store / folder / name, (i, i))
    assert _runner(harness, tmp_path, cwd).prepare_resume(SID) is True
    assert (tmp_path / store / new / name).read_text() == old


@pytest.mark.parametrize("harness", CASES)
def test_a_session_that_is_gone_says_so(tmp_path, harness):
    assert _runner(harness, tmp_path, CASES[harness][4]).prepare_resume(SID) is False


@pytest.mark.parametrize("harness", CASES)
@pytest.mark.parametrize("bad", ["*", "../x", "a/b", "[a]", ""])
def test_an_id_that_is_not_an_id_never_reaches_the_glob(tmp_path, harness, bad):
    assert _runner(harness, tmp_path, CASES[harness][4]).prepare_resume(bad) is False
