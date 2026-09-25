"""Tests for the privilege-drop launcher (``docker/cron/launch.sh``).

Every test **executes** the script. Because the launcher re-execs through `env -i` with a
fixed PATH, the copy under test is the real script with its two absolute prefixes rewritten
to the temp dir (`/opt/cron-agent` and `/workspace`) and the stub dir added to that fixed
PATH — nothing else is changed, and Task 11 verifies the original in the container.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

LAUNCH_SH = (
    Path(__file__).resolve().parents[3]
    / "src/agento/framework/docker/cron/launch.sh"
)

CANARIES = {
    "MYSQL_HOST": "canary-mysql-host",
    "MYSQL_PASSWORD": "canary-mysql-password",
    "AGENTO_ENCRYPTION_KEY": "canary-encryption-key",
    "CONFIG__JIRA__API_TOKEN": "canary-config-token",
}


@pytest.fixture
def launcher(tmp_path):
    """The real script, relocated into ``tmp_path``, with stubs for ``setpriv``."""
    root = tmp_path / "cron-agent"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()

    # The launcher scrubs the environment, so every output path is baked into the stubs
    # as a literal — a variable would be (correctly) wiped before the stub ever runs.
    argv_out = tmp_path / "argv.txt"
    env_out = tmp_path / "env.txt"
    grandchild_env_out = tmp_path / "grandchild-env.txt"
    setpriv_args = tmp_path / "setpriv-args.txt"

    setpriv = stubs / "setpriv"
    setpriv.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        # Record the drop arguments, then run the rest (we cannot really change uid).
        printf '%s\\n' "$1 $2 $3 $4 $5" > {setpriv_args}
        shift 5
        exec "$@"
    """))
    setpriv.chmod(0o755)

    # The launcher resolves the target ids itself; the host has no `agent` account.
    id_stub = stubs / "id"
    id_stub.write_text(textwrap.dedent("""\
        #!/bin/bash
        [ "$2" = agent ] || { exec /usr/bin/id "$@"; }
        case "$1" in -u) echo 4242 ;; -g) echo 4343 ;; *) exec /usr/bin/id "$@" ;; esac
    """))
    id_stub.chmod(0o755)

    # The --store branch hands the command to root-owned drop.py through the venv python.
    (root / "run.sh").write_text("#!/bin/bash\nexit 1\n")
    (root / "run.sh").chmod(0o755)
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    drop_argv = tmp_path / "drop-argv.txt"
    venv_python.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        printf '%s\\n' "$@" > {drop_argv}
        env > {env_out}
        /bin/sh -c 'env > {grandchild_env_out}'
    """))
    venv_python.chmod(0o755)

    script = tmp_path / "launch.sh"
    script.write_text(
        LAUNCH_SH.read_text()
        .replace("/opt/cron-agent", str(root))
        .replace("cd /workspace", f"cd {workspace}")
        .replace(
            "PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            f"PATH={stubs}:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        )
    )
    script.chmod(0o755)

    dump = tmp_path / "dump.sh"
    dump.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        printf '%s\\n' "$@" > {argv_out}
        env > {env_out}
        # A grandchild inherits whatever we hold — assert on it too.
        /bin/sh -c 'env > {grandchild_env_out}'
    """))
    dump.chmod(0o755)

    class Harness:
        def __init__(self):
            self.root = root
            self.script = script
            self.dump = dump
            self.workspace = workspace
            self.argv_out = argv_out
            self.env_out = env_out
            self.grandchild_env_out = grandchild_env_out
            self.setpriv_args = setpriv_args
            self.drop_argv = drop_argv
            self.run_sh = root / "run.sh"

        def public(self, payload: bytes) -> None:
            (root / "env.public").write_bytes(payload)

        def store(self, payload: bytes) -> None:
            (root / "env").write_bytes(payload)

        def run(self, *args, env_extra=None):
            env = {**os.environ, **(env_extra or {})}
            return subprocess.run(
                [str(self.script), *args], capture_output=True, text=True, env=env,
            )

        @property
        def child_env(self) -> dict[str, str]:
            return _parse_env(self.env_out)

        @property
        def grandchild_env(self) -> dict[str, str]:
            return _parse_env(self.grandchild_env_out)

    harness = Harness()
    harness.public(b"")
    harness.store(b"MYSQL_HOST=db\0")
    return harness


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep:
            out[name] = value
    return out


# --- argv contract --------------------------------------------------------------------

def test_the_command_receives_exactly_the_argv_it_was_given(launcher):
    result = launcher.run("--", str(launcher.dump), "a b", "c'd")
    assert result.returncode == 0, result.stderr
    assert launcher.argv_out.read_text().splitlines() == ["a b", "c'd"]


def test_bad_usage_exits_64(launcher):
    assert launcher.run(str(launcher.dump)).returncode == 64
    assert launcher.run("--store", str(launcher.dump)).returncode == 64
    assert launcher.run("--").returncode == 64


def test_the_privilege_drop_never_uses_reset_env(launcher):
    launcher.run("--", str(launcher.dump))
    assert launcher.setpriv_args.read_text().strip() == (
        "--reuid 4242 --regid 4343 --init-groups"
    )
    assert "--reset-env" not in LAUNCH_SH.read_text()


def test_the_store_branch_hands_the_command_to_the_dropper(launcher):
    """`drop.py` drops in-process, so the store crosses no exec — nothing else may run."""
    result = launcher.run("--store", "--", str(launcher.run_sh), "consumer")

    assert result.returncode == 0, result.stderr
    argv = launcher.drop_argv.read_text().split()
    assert argv[0].endswith("drop.py")
    assert argv[1:] == ["consumer"]
    assert "AGENTO_STORE_ENV_FD" not in launcher.child_env


def test_the_store_runs_nothing_but_the_cli(launcher):
    result = launcher.run("--store", "--", str(launcher.dump), "whoami")

    assert result.returncode == 64
    assert "run.sh" in result.stderr


def test_the_login_environment_su_used_to_supply_is_rebuilt(launcher):
    launcher.run("--", str(launcher.dump))
    assert launcher.child_env["HOME"] == "/home/agent"
    assert launcher.child_env["USER"] == "agent"


# --- the class this change exists for: no store in any inherited environment ------------

@pytest.mark.parametrize("store_flag", [[], ["--store"]])
def test_no_store_canary_reaches_the_command_or_its_grandchild(launcher, store_flag):
    command = str(launcher.run_sh) if store_flag else str(launcher.dump)
    launcher.run(*store_flag, "--", command, env_extra=CANARIES)
    for env in (launcher.child_env, launcher.grandchild_env):
        for name, value in CANARIES.items():
            assert name not in env
            assert value not in "".join(env.values())


def test_an_inherited_clean_marker_cannot_skip_the_scrub(launcher):
    """The guard is a positional argument, so no inherited variable can fake it."""
    launcher.run(
        "--", str(launcher.dump),
        env_extra={**CANARIES, "LAUNCH_CLEAN": "1", "LAUNCH_CLEAN_INTERNAL": "1"},
    )
    for name in CANARIES:
        assert name not in launcher.child_env


# --- env.public import ------------------------------------------------------------------

def test_public_values_reach_the_command(launcher):
    launcher.public(b"TZ=Europe/Warsaw\0AGENTO_CONSUMER_MAX_WORKERS=4\0")
    launcher.run("--", str(launcher.dump))
    assert launcher.child_env["TZ"] == "Europe/Warsaw"
    assert launcher.child_env["AGENTO_CONSUMER_MAX_WORKERS"] == "4"


def test_a_malformed_name_is_skipped_and_the_launch_still_succeeds(launcher):
    """A glob guard accepts ``A-B=x``; ``export`` then fails and ``set -e`` would abort."""
    launcher.public(b"A-B=x\0TZ=UTC\0noequalshere\0=novalue\0")
    result = launcher.run("--", str(launcher.dump))
    assert result.returncode == 0, result.stderr
    assert launcher.child_env["TZ"] == "UTC"
    assert "A-B" not in launcher.child_env


def test_public_values_arrive_byte_identical_and_never_execute(launcher, tmp_path):
    side_effect = tmp_path / "pwned"
    values = {
        "SPACES": "two words",
        "QUOTES": "it's \"quoted\"",
        "NEWLINE": "one\ntwo",
        "SUBST": f"$(touch {side_effect}) `touch {side_effect}`",
        "EQUALS": "a=b=c",
    }
    payload = b"".join(f"{k}={v}".encode() + b"\0" for k, v in values.items())
    launcher.public(payload)
    result = launcher.run("--", str(launcher.dump))
    assert result.returncode == 0, result.stderr

    env = launcher.child_env
    assert env["SPACES"] == "two words"
    assert env["QUOTES"] == "it's \"quoted\""
    assert env["SUBST"] == values["SUBST"]
    assert env["EQUALS"] == "a=b=c"
    assert not side_effect.exists()
    # The newline value survives whole — `env` prints it across two lines.
    assert "one\ntwo" in launcher.env_out.read_text()


def test_the_drop_resolves_numeric_ids(launcher):
    """`setpriv --regid agent` fails outright — no group is named `agent` in the image."""
    launcher.run("--", str(launcher.dump))
    _, uid, _, gid, _ = launcher.setpriv_args.read_text().split()
    assert uid.isdigit() and gid.isdigit()
