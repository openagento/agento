"""Guards over the cron image: what runs as root, and what uid ``agent`` can read.

These assert the *shape* of the entrypoint and Dockerfile, because the property they
defend — no path readable at uid ``agent`` yields the credential store — is a property of
those two files, and a regression in either is silent until someone reads a peer's key.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

DOCKER_CRON = (
    Path(__file__).resolve().parents[3] / "src/agento/framework/docker/cron"
)
ENTRYPOINT = (DOCKER_CRON / "entrypoint.sh").read_text()
DOCKERFILE = (DOCKER_CRON / "Dockerfile").read_text()
LAUNCH_SH = (DOCKER_CRON / "launch.sh").read_text()


def _load_splitter():
    spec = importlib.util.spec_from_file_location(
        "split_env", DOCKER_CRON / "split-env.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["split_env"] = module
    spec.loader.exec_module(module)
    return module


splitter = _load_splitter()


# --- the replaced boundary --------------------------------------------------------------

def test_the_entrypoint_never_drops_privileges_with_su():
    """``su -`` wipes the environment, which is the only reason a readable env file existed."""
    assert "su - agent" not in ENTRYPOINT
    assert "su -" not in ENTRYPOINT


def test_every_privilege_drop_goes_through_the_launcher():
    drops = [ln for ln in ENTRYPOINT.splitlines() if "/opt/cron-agent/run.sh" in ln]
    assert drops
    for line in drops:
        assert "/opt/cron-agent/launch.sh" in line


def test_the_store_file_is_never_world_readable():
    assert "chmod 644" not in ENTRYPOINT
    assert re.search(r"chmod\s+0?644\s+\"?\$?ENV_FILE", ENTRYPOINT) is None
    # The splitter creates it 0600 before the first byte lands in it.
    source = (DOCKER_CRON / "split-env.py").read_text()
    assert "0o600" in source


def test_the_agent_owns_nothing_under_opt_cron_agent():
    assert "chown -R agent /opt/cron-agent" not in DOCKERFILE
    assert "chown -R root:root /opt/cron-agent" in DOCKERFILE
    for program in ("launch.sh", "install-crontab.py", "split-env.py", "drop.py"):
        assert f"/opt/cron-agent/{program}" in DOCKERFILE
    assert "chmod 0700" in DOCKERFILE


def test_the_agent_has_no_crontab():
    assert "crontab -u agent -" not in ENTRYPOINT.replace("crontab -u agent -r", "")
    assert "crontab -u agent -r" in ENTRYPOINT  # any pre-existing one is removed


def test_the_drop_uses_setpriv_without_reset_env():
    assert "setpriv" in LAUNCH_SH
    assert "--reset-env" not in LAUNCH_SH


def test_the_store_never_crosses_an_exec():
    """`execve` resets PR_SET_DUMPABLE, so a store handed to an exec'd image is readable
    by a same-uid ptrace for the whole of Python's startup. The `--store` branch must
    therefore hand the command to `drop.py`, which drops in-process, and must pass no
    descriptor or variable of its own."""
    store_branch = LAUNCH_SH[LAUNCH_SH.index('want_store" = 1'):]

    assert "drop.py" in store_branch
    assert "setpriv" not in store_branch.split("fi", 1)[0]
    assert "exec 9<" not in LAUNCH_SH
    assert "AGENTO_STORE_ENV_FD" not in LAUNCH_SH


def test_the_store_reader_drops_before_it_runs_anything():
    source = (DOCKER_CRON / "drop.py").read_text()

    drop_at = source.index("_drop_to(AGENT_USER)")
    assert source.index("make_non_dumpable()", drop_at) > drop_at
    assert source.index("store_env.load(raw)", drop_at) > drop_at
    assert source.index("cli_main()", drop_at) > drop_at
    # An exec anywhere past the drop would reopen the window the drop closes.
    assert "exec" not in source.split("def _drop_to", 1)[1].replace("execve", "")


def test_the_renderer_runs_after_setup_upgrade():
    """A fresh database has no ``schedule`` table until migrations have run."""
    setup_at = ENTRYPOINT.index("setup:upgrade --skip-onboarding")
    render_at = ENTRYPOINT.index("install-crontab.py", setup_at)
    assert render_at > setup_at


def test_the_bootstrap_installer_line_matches_the_renderers_own():
    """Root re-emits the line that invokes it; a drift would stop the renderer."""
    renderer_spec = importlib.util.spec_from_file_location(
        "ic_line", DOCKER_CRON / "install-crontab.py",
    )
    renderer = importlib.util.module_from_spec(renderer_spec)
    sys.modules["ic_line"] = renderer
    renderer_spec.loader.exec_module(renderer)
    assert renderer.INSTALLER_LINE in ENTRYPOINT


# --- the partition ------------------------------------------------------------------------

STORE_CANARIES = {
    "MYSQL_HOST": "db",
    "MYSQL_PASSWORD": "p@ss",
    "AGENTO_ENCRYPTION_KEY": "k",
    "CONFIG__JIRA__API_TOKEN": "t",
}
PUBLIC_CANARIES = {
    "TZ": "Europe/Warsaw",
    "PYTHONPATH": "/opt/agento-src",
    "PROVIDER": "claude",
    "DISABLE_LLM": "0",
    "AGENTO_CONSUMER_MAX_WORKERS": "10",
}


def _payload(*mappings) -> bytes:
    return b"".join(
        f"{k}={v}".encode() + b"\0" for m in mappings for k, v in m.items()
    )


def _records(blob: bytes) -> dict[str, str]:
    out = {}
    for rec in blob.split(b"\0"):
        if rec:
            name, _, value = rec.decode().partition("=")
            out[name] = value
    return out


def test_the_two_files_partition_the_whitelist():
    store, public = splitter.partition(_payload(STORE_CANARIES, PUBLIC_CANARIES))
    assert _records(store) == STORE_CANARIES
    assert _records(public) == PUBLIC_CANARIES


def test_no_store_shape_can_land_on_the_public_side():
    """The guard that stops a secret added later from quietly going public."""
    from agento.framework.credential_store_env import is_credential_store_name

    _, public = splitter.partition(_payload(STORE_CANARIES, PUBLIC_CANARIES))
    assert not any(is_credential_store_name(n) for n in _records(public))


def test_a_name_outside_the_whitelist_reaches_neither_file():
    store, public = splitter.partition(_payload({"HOSTNAME": "x", "LS_COLORS": "y"}))
    assert store == b""
    assert public == b""


@pytest.mark.parametrize("value", ["one\ntwo", 'a "quoted" b', "a=b=c", "$(id)"])
def test_a_value_survives_the_split_byte_identical(value):
    store, public = splitter.partition(
        _payload({"MYSQL_PASSWORD": value, "AGENTO_X": value})
    )
    assert _records(store)["MYSQL_PASSWORD"] == value
    assert _records(public)["AGENTO_X"] == value


def test_the_written_store_file_is_not_readable_by_anyone_else(tmp_path):
    store_file = tmp_path / "env"
    public_file = tmp_path / "env.public"
    splitter.STORE_FILE = str(store_file)
    splitter.PUBLIC_FILE = str(public_file)

    class _Stdin:
        buffer = type("B", (), {"read": staticmethod(lambda: _payload(STORE_CANARIES))})()

    real_stdin, sys.stdin = sys.stdin, _Stdin()
    try:
        splitter.main()
    finally:
        sys.stdin = real_stdin

    assert store_file.stat().st_mode & 0o077 == 0
    assert _records(store_file.read_bytes()) == STORE_CANARIES


def test_an_existing_world_readable_store_file_is_tightened(tmp_path):
    """A pre-V0 image left it 0644; `O_TRUNC` alone would keep that mode."""
    store_file = tmp_path / "env"
    store_file.write_bytes(b"")
    store_file.chmod(0o644)
    splitter.STORE_FILE = str(store_file)
    splitter.PUBLIC_FILE = str(tmp_path / "env.public")

    class _Stdin:
        buffer = type("B", (), {"read": staticmethod(lambda: _payload(STORE_CANARIES))})()

    real_stdin, sys.stdin = sys.stdin, _Stdin()
    try:
        splitter.main()
    finally:
        sys.stdin = real_stdin

    assert store_file.stat().st_mode & 0o077 == 0
