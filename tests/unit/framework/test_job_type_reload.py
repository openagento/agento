"""The job-type registry across a bootstrap reload (PRD E3-E5 §4.2, MOD-1).

The consumer re-runs bootstrap() every poll interval when idle, so a registry that is
not cleared accumulates - and since register_job_type raises on a duplicate, the SECOND
bootstrap would crash on the declaration that succeeded the first time.
"""

import json
from pathlib import Path

import pytest

from agento.framework.bootstrap import bootstrap
from agento.framework.job_types import BUILTIN_JOB_TYPES, JobTypeUnknown, resolve_job_type

WF_SRC = (
    "from agento.framework.workflows.base import Workflow\n"
    "class ConvWorkflow(Workflow):\n"
    "    def build_prompt(self, channel, ref, **kw): return 'x'\n"
)


def _write_conv_module(modules_dir: Path) -> Path:
    mod = modules_dir / "conversation"
    (mod / "src").mkdir(parents=True)
    (mod / "module.json").write_text(json.dumps({
        "name": "conversation",
        "provides": {
            "job_types": ["conversation"],
            "workflows": [{"type": "conversation", "class": "src.wf.ConvWorkflow"}],
        },
    }))
    (mod / "src" / "wf.py").write_text(WF_SRC)
    return mod


def test_second_bootstrap_does_not_raise_on_the_same_declaration(tmp_path: Path):
    _write_conv_module(tmp_path)
    bootstrap(str(tmp_path))
    bootstrap(str(tmp_path))          # the hot-reload path
    assert resolve_job_type("conversation").value == "conversation"


def test_disabling_the_module_unregisters_its_type(tmp_path: Path):
    mod = _write_conv_module(tmp_path)
    bootstrap(str(tmp_path))
    assert resolve_job_type("conversation").value == "conversation"

    (mod / "module.json").unlink()     # module gone from discovery
    bootstrap(str(tmp_path))
    with pytest.raises(JobTypeUnknown):
        resolve_job_type("conversation")


def test_re_enabling_brings_the_type_back(tmp_path: Path):
    mod = _write_conv_module(tmp_path)
    manifest = (mod / "module.json").read_text()
    bootstrap(str(tmp_path))
    (mod / "module.json").unlink()
    bootstrap(str(tmp_path))
    (mod / "module.json").write_text(manifest)
    bootstrap(str(tmp_path))
    assert resolve_job_type("conversation").value == "conversation"


def test_builtins_survive_a_reload_that_finds_nothing(tmp_path: Path):
    """The built-ins are not in the registry, so an empty or failed reload still resolves them."""
    bootstrap(str(tmp_path))
    for value in BUILTIN_JOB_TYPES:
        assert resolve_job_type(value).value == value
