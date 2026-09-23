"""Tests for the SSH prelude wrapper — the agent gets a socket, never a key.

Split by host requirement. ``bin/test`` runs pytest on the HOST, and the supported dev
host is macOS with bash 3.2.57 and no ``/proc`` — below the floor the wrapper is verified
against, and the runtime never executes on the host anyway (the consumer spawns inside the
cron container, ``agento run`` goes through ``docker compose exec … sandbox``). So the
version-free cases — the generated text, the ORDER of its parts, and the three failure
branches — always run, and only the pipe-backed happy path is ``skipif``-guarded. The
floor itself is enforced in the image build, not by a host test.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from agento.framework.ssh_prelude import (
    PASSWD_HOME_SSH_DIR,
    ssh_prelude_script,
    wrap_with_ssh_prelude,
)

_REPO = Path(__file__).resolve().parents[3]
_DOCKER = _REPO / "src" / "agento" / "framework" / "docker"


def _code_lines(text: str) -> str:
    """Only the lines that DO something — comments explain, they do not create."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _host_bash_below(minimum: str) -> bool:
    try:
        out = subprocess.check_output(["bash", "-c", "echo $BASH_VERSION"], text=True)
    except (OSError, subprocess.CalledProcessError):
        return True
    parts = re.findall(r"\d+", out)
    have = tuple(int(p) for p in parts[:2]) if len(parts) >= 2 else (0, 0)
    want = tuple(int(p) for p in minimum.split("."))
    return have < want


_NEEDS_IMAGE_BASH = pytest.mark.skipif(
    _host_bash_below("5.1") or not Path("/proc/self/fd").exists(),
    reason=(
        "the wrapper is verified against bash >= 5.1 with /proc; the runtime ALWAYS runs "
        "in the image, where the build asserts both — a skip here is not a coverage hole"
    ),
)


def _stub_bin(
    tmp_path: Path, *, ssh_add_exit: int = 0, record: Path | None = None,
    probe_fs: bool = False,
) -> Path:
    """A PATH dir with stub ``ssh-agent``/``ssh-add``, mimicking command mode."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent = bin_dir / "ssh-agent"
    agent.write_text(
        "#!/bin/bash\n"
        'echo "AGENT_ARGV:$1 $2" >> "$AGENTO_TEST_RECORD"\n'
        "export SSH_AUTH_SOCK=/tmp/agento-test.sock\n"
        "shift 2\n"
        'exec "$@"\n'
    )
    add = bin_dir / "ssh-add"
    # The probe lines run WHILE ssh-add holds the key on fd 0 — i.e. while the process
    # substitution is live. A scan performed only after the wrapper returns cannot tell a
    # never-created FIFO from one that was created, read and unlinked.
    probe = (
        'echo "FIFOS:$(find "${TMPDIR:-/tmp}" "$AGENTO_TEST_SCAN" -maxdepth 2 -type p'
        ' -name "sh-*"'
        ' 2>/dev/null | wc -l | tr -d " ")" >> "$AGENTO_TEST_RECORD"\n'
        'echo "LEAK:$(grep -rl -F -- "$AGENTO_TEST_MARKER" "$AGENTO_TEST_SCAN"'
        ' 2>/dev/null | grep -v record.txt | tr "\\n" " ")" >> "$AGENTO_TEST_RECORD"\n'
    ) if probe_fs else ""
    add.write_text(
        "#!/bin/bash\n"
        'echo "FD0:$(readlink /proc/self/fd/0)" >> "$AGENTO_TEST_RECORD"\n'
        + probe +
        'echo "KEY:$(cat)" >> "$AGENTO_TEST_RECORD"\n'
        f"exit {ssh_add_exit}\n"
    )
    for f in (agent, add):
        f.chmod(0o755)
    return bin_dir


def _run(cmd: list[str], tmp_path: Path, **env_extra):
    record = tmp_path / "record.txt"
    record.touch()
    env = {
        **os.environ,
        "AGENTO_TEST_RECORD": str(record),
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        **env_extra,
    }
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True)
    return proc, record.read_text()


def _wrapped(cmd: list[str], passwd_ssh: Path) -> list[str]:
    return wrap_with_ssh_prelude(cmd, passwd_home_ssh_dir=str(passwd_ssh))


class TestGeneratedText:
    def test_wraps_with_bash_c_and_arg0(self):
        wrapped = wrap_with_ssh_prelude(["claude", "-p", "hi"])
        # bash, not sh: the key rides a process substitution.
        assert wrapped[:2] == ["bash", "-c"]
        assert wrapped[3] == "agento-ssh-prelude"
        assert wrapped[4:] == ["claude", "-p", "hi"]

    def test_delivers_the_key_by_process_substitution_not_a_here_string(self):
        script = ssh_prelude_script()
        assert "3< <(printf" in script
        # A here-string is weighed against HEREDOC_PIPESIZE and can fall back to a TEMP
        # FILE — the dependency the redesign removed. It must not come back.
        assert "3<<<" not in script

    def test_preflight_precedes_the_secret_bearing_substitution(self):
        """The round-10 defect: a guard placed AFTER the event it guards.

        bash creates a process substitution during EXPANSION, so a check that lives inside
        ``ssh-agent`` runs only once a FIFO-backed substitution would already exist WITH
        THE KEY IN IT. This is a POSITIONAL assertion because the defect is an ordering
        one, not a behavioural one.
        """
        script = ssh_prelude_script()
        definition = script.index("substitution_is_pipe() (")
        call = script.index("] && substitution_is_pipe; then")
        key_substitution = script.index('3< <(printf \'%s\\n\' "$AGENTO_SSH_PRIVATE_KEY")')
        assert definition < call < key_substitution

    def test_scrubs_the_key_and_the_socket_on_every_exit_path(self):
        script = ssh_prelude_script()
        # Every path that does not load an identity leaves NO SSH_AUTH_SOCK: a socket to
        # an empty agent is a half-identity.
        assert 'exec env -u SSH_AUTH_SOCK "$@"' in script
        assert "unset SSH_AUTH_SOCK" in script
        # And no path hands the key to the agent command.
        assert 'exec env -u AGENTO_SSH_PRIVATE_KEY "$@"' in script

    def test_never_creates_or_symlinks_a_passwd_home_ssh_dir(self):
        script = ssh_prelude_script()
        assert "ln -s" not in script
        assert f"rm -rf {PASSWD_HOME_SSH_DIR}" in script
        # It cannot touch /root as uid agent, so it must not pretend to: a `|| true`
        # there would be a check that always passes. The entrypoints own that path.
        # Assert on the CODE, not on the prose that explains why.
        assert "/root" not in _code_lines(script)

    def test_key_reaches_argv_of_nothing(self):
        script = ssh_prelude_script()
        # The only appearances of the variable are the emptiness tests, the `env -u`
        # scrubs and the printf inside the substitution — never an argument to an exec'd
        # program, so it cannot reach any /proc/<pid>/cmdline.
        for line in script.splitlines():
            if "AGENTO_SSH_PRIVATE_KEY" not in line:
                continue
            assert (
                '${AGENTO_SSH_PRIVATE_KEY:-}' in line
                or "env -u AGENTO_SSH_PRIVATE_KEY" in line
                or 'printf \'%s\\n\' "$AGENTO_SSH_PRIVATE_KEY"' in line
            ), line


class TestImageAndEntrypointGuards:
    """The properties AC4 rests on are asserted where they apply: in the image."""

    @pytest.mark.parametrize("name", ["sandbox/Dockerfile", "../cli/templates/sandbox.Dockerfile"])
    def test_dockerfile_asserts_pipe_backed_substitution_and_bash_floor(self, name):
        text = (_DOCKER / name).read_text()
        assert "exec 3< <(printf x)" in text
        assert "pipe:*" in text
        assert "BASH_VERSINFO[0]" in text

    @pytest.mark.parametrize("name", ["sandbox/Dockerfile", "../cli/templates/sandbox.Dockerfile"])
    def test_image_does_not_create_a_passwd_home_ssh_dir(self, name):
        # The instruction lines, not the comment that explains why the path is absent.
        assert "/home/agent/.ssh" not in _code_lines((_DOCKER / name).read_text())

    @pytest.mark.parametrize("name", ["sandbox/entrypoint.sh", "cron/entrypoint.sh"])
    def test_both_entrypoints_clear_the_passwd_homes_and_fail_closed(self, name):
        """cron/Dockerfile sets its OWN ENTRYPOINT, so the cron copy is the one that
        matters at run time — a test checking only the sandbox copy would pass while the
        shipped behaviour was missing."""
        text = (_DOCKER / name).read_text()
        assert "/home/agent/.ssh /root/.ssh" in text
        assert "exit 78" in text

    @pytest.mark.parametrize("name", ["sandbox/entrypoint.sh", "cron/entrypoint.sh"])
    def test_both_entrypoints_refuse_an_ambient_run_owned_ssh_variable(self, name):
        """Coupled to the Python constant, so adding a name there cannot leave the
        container boundary behind: an ambient value would be inherited by EVERY view's
        runs, including views granted no key."""
        from agento.framework.ssh_identity import RUN_OWNED_SSH_ENV_VARS

        guard = _code_lines((_DOCKER / name).read_text())
        for var in RUN_OWNED_SSH_ENV_VARS:
            assert var in guard, f"{name} does not refuse an inherited {var}"
        # The ENV channel cannot carry a multiline PEM across `su - agent` and would put
        # the key in a file if it could, so that config path is refused outright.
        assert "CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY" in guard


class TestPreludeFailurePaths:
    """The three paths that must never hand the agent a key or a half-identity."""

    def test_no_key_configured_is_a_pure_exec_and_unsets_an_inherited_socket(self, tmp_path):
        _stub_bin(tmp_path)
        cmd = ["sh", "-c", "echo RAN; env | grep -c -e AGENTO_SSH_PRIVATE_KEY -e SSH_AUTH_SOCK || true"]
        proc, record = _run(
            _wrapped(cmd, tmp_path / "nohome" / ".ssh"), tmp_path,
            AGENTO_SSH_PRIVATE_KEY="", SSH_AUTH_SOCK="/inherited/peer.sock",
        )
        assert proc.returncode == 0
        assert "RAN" in proc.stdout
        assert proc.stdout.strip().splitlines()[-1] == "0"
        assert "AGENT_ARGV" not in record  # no agent started at all

    def test_exit_status_is_forwarded(self, tmp_path):
        _stub_bin(tmp_path)
        proc, _ = _run(
            _wrapped(["sh", "-c", "exit 42"], tmp_path / "nohome" / ".ssh"), tmp_path,
            AGENTO_SSH_PRIVATE_KEY="",
        )
        assert proc.returncode == 42

    def test_stdin_is_untouched(self, tmp_path):
        _stub_bin(tmp_path)
        wrapped = _wrapped(["sh", "-c", "cat"], tmp_path / "nohome" / ".ssh")
        proc = subprocess.run(
            wrapped, input="caller-stdin", text=True, capture_output=True,
            env={**os.environ, "AGENTO_SSH_PRIVATE_KEY": "",
                 "AGENTO_TEST_RECORD": str(tmp_path / "r.txt"),
                 "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"},
        )
        assert proc.stdout == "caller-stdin"

    def test_planted_identity_is_removed_and_the_run_proceeds(self, tmp_path):
        _stub_bin(tmp_path)
        planted = tmp_path / "passwd_home" / ".ssh"
        planted.mkdir(parents=True)
        (planted / "id_rsa").write_text("PEER-KEY")
        proc, _ = _run(
            _wrapped(["sh", "-c", "echo RAN"], planted), tmp_path,
            AGENTO_SSH_PRIVATE_KEY="",
        )
        assert proc.returncode == 0
        assert "RAN" in proc.stdout
        assert not planted.exists()

    def test_planted_identity_that_cannot_be_removed_aborts_with_78(self, tmp_path):
        _stub_bin(tmp_path)
        parent = tmp_path / "locked_home"
        planted = parent / ".ssh"
        planted.mkdir(parents=True)
        (planted / "id_rsa").write_text("PEER-KEY")
        parent.chmod(0o500)  # unlink denied for the owner too
        try:
            proc, _ = _run(
                _wrapped(["sh", "-c", "echo SHOULD_NOT_RUN"], planted), tmp_path,
                AGENTO_SSH_PRIVATE_KEY="",
            )
        finally:
            parent.chmod(0o700)
        assert proc.returncode == 78
        assert "SHOULD_NOT_RUN" not in proc.stdout
        assert "refusing to run" in proc.stderr

    def test_a_non_pipe_backed_platform_drops_the_key_instead_of_delivering_it(
        self, tmp_path,
    ):
        """The preflight's own contract, exercised by shimming ``readlink``.

        Where ``<(...)`` is FIFO-backed the key would become a filesystem object, so the
        wrapper must never construct the key-bearing substitution at all: no ssh-add, no
        socket, no key in the agent's environment — and a warning naming the mechanism.
        """
        bin_dir = _stub_bin(tmp_path)
        shim = bin_dir / "readlink"
        shim.write_text("#!/bin/bash\necho /tmp/not-a-pipe\n")
        shim.chmod(0o755)
        cmd = ["sh", "-c", "echo RAN; env | grep -c -e AGENTO_SSH_PRIVATE_KEY -e SSH_AUTH_SOCK || true"]
        proc, record = _run(
            _wrapped(cmd, tmp_path / "nohome" / ".ssh"), tmp_path,
            AGENTO_SSH_PRIVATE_KEY="-----BEGIN OPENSSH PRIVATE KEY-----\nx\n",
        )
        assert "refusing to deliver the SSH key" in proc.stderr
        assert "RAN" in proc.stdout
        assert proc.stdout.strip().splitlines()[-1] == "0"
        assert "FD0:" not in record  # ssh-add was never invoked
        assert "AGENT_ARGV" not in record


    @_NEEDS_IMAGE_BASH
    def test_an_unavailable_ssh_agent_fails_the_run_instead_of_dropping_the_key(
        self, tmp_path,
    ):
        """No agent binary => no signing capability. The run must NOT continue key-less:
        a job that silently cannot push looks like a code failure to everyone."""
        bin_dir = _stub_bin(tmp_path)
        (bin_dir / "ssh-agent").write_text("#!/bin/bash\nexit 127\n")  # as if absent
        (bin_dir / "ssh-agent").chmod(0o755)
        proc, record = _run(
            _wrapped(["sh", "-c", "echo SHOULD_NOT_RUN"], tmp_path / "nohome" / ".ssh"),
            tmp_path, AGENTO_SSH_PRIVATE_KEY="KEYDATA",
        )
        assert proc.returncode == 127
        assert "SHOULD_NOT_RUN" not in proc.stdout
        assert "KEY:" not in record

    def test_fd3_that_is_not_a_pipe_is_refused_inside_the_agent_too(self, tmp_path):
        """Defence in depth: the preflight passes, the INNER check still refuses.

        Shimmed ``readlink`` reports a pipe for the preflight's fd 4 and a path for fd 3,
        which is exactly the FIFO-backed case — the key must not be loaded, and the
        command must run with no identity rather than with a filesystem-backed one.
        """
        bin_dir = _stub_bin(tmp_path)
        shim = bin_dir / "readlink"
        shim.write_text(
            "#!/bin/bash\n"
            'case "$1" in */fd/4) echo "pipe:[999]";; *) echo /tmp/sh-thd-fake;; esac\n'
        )
        shim.chmod(0o755)
        cmd = ["sh", "-c", "echo RAN; env | grep -c -e AGENTO_SSH_PRIVATE_KEY -e SSH_AUTH_SOCK || true"]
        proc, record = _run(
            _wrapped(cmd, tmp_path / "nohome" / ".ssh"), tmp_path,
            AGENTO_SSH_PRIVATE_KEY="KEYDATA",
        )
        assert "not an anonymous pipe" in proc.stderr
        assert "FD0:" not in record  # ssh-add never ran
        assert "RAN" in proc.stdout
        assert proc.stdout.strip().splitlines()[-1] == "0"


@_NEEDS_IMAGE_BASH
class TestPreludeHappyPath:
    def test_key_reaches_ssh_add_over_an_anonymous_pipe_and_never_a_file(self, tmp_path):
        _stub_bin(tmp_path)
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETBODY\n-----END OPENSSH PRIVATE KEY-----"
        cmd = ["sh", "-c", "echo RAN; env | grep -c AGENTO_SSH_PRIVATE_KEY || true"]
        proc, record = _run(
            _wrapped(cmd, tmp_path / "nohome" / ".ssh"), tmp_path,
            AGENTO_SSH_PRIVATE_KEY=key, AGENTO_SSH_TTL="1234",
        )
        assert proc.returncode == 0
        # Delivered on a PIPE, not a path under /tmp or anywhere else.
        fd0 = [ln for ln in record.splitlines() if ln.startswith("FD0:")]
        assert fd0 and fd0[0].startswith("FD0:pipe:[")
        assert "SECRETBODY" in record  # the key really was loaded
        assert "AGENT_ARGV:-t 1234" in record  # the caller's TTL, not the fallback
        assert proc.stdout.strip().splitlines()[-1] == "0"  # key gone from the agent env
        # And nothing under the temp tree holds the key material.
        for path in tmp_path.rglob("*"):
            if path.is_file() and path.name != "record.txt":
                assert "SECRETBODY" not in path.read_text(errors="ignore")

    @pytest.mark.parametrize("size", [1, 16384])
    def test_pipe_backing_does_not_depend_on_key_size(self, tmp_path, size):
        """The property a here-string could not give: no threshold, no temp-file fallback."""
        _stub_bin(tmp_path)
        proc, record = _run(
            _wrapped(["true"], tmp_path / "nohome" / ".ssh"), tmp_path,
            AGENTO_SSH_PRIVATE_KEY="k" * size,
        )
        assert proc.returncode == 0
        fd0 = [ln for ln in record.splitlines() if ln.startswith("FD0:")]
        assert fd0 and fd0[0].startswith("FD0:pipe:[")

    def test_fd3_is_closed_before_the_agent_command_runs(self, tmp_path):
        _stub_bin(tmp_path)
        proc, _ = _run(
            _wrapped(["sh", "-c", "test -e /proc/self/fd/3 && echo OPEN || echo CLOSED"],
                     tmp_path / "nohome" / ".ssh"),
            tmp_path, AGENTO_SSH_PRIVATE_KEY="KEYDATA",
        )
        assert proc.stdout.strip() == "CLOSED"

    def test_ssh_add_failure_leaves_no_identity_and_no_socket(self, tmp_path):
        _stub_bin(tmp_path, ssh_add_exit=1)
        cmd = ["sh", "-c", "echo RAN; env | grep -c -e AGENTO_SSH_PRIVATE_KEY -e SSH_AUTH_SOCK || true"]
        proc, _ = _run(
            _wrapped(cmd, tmp_path / "nohome" / ".ssh"), tmp_path,
            AGENTO_SSH_PRIVATE_KEY="KEYDATA",
        )
        assert "ssh-add failed" in proc.stderr
        assert "RAN" in proc.stdout
        assert proc.stdout.strip().splitlines()[-1] == "0"

    def test_no_filesystem_object_holds_the_key_WHILE_it_is_being_loaded(self, tmp_path):
        """The `never a file` claim, asserted during the only window it could be false."""
        _stub_bin(tmp_path, probe_fs=True)
        marker = "SECRETBODYWHILELOADING"
        key = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{marker}\n"
        record_path = tmp_path / "record.txt"
        record_path.touch()
        env = {
            **os.environ,
            "AGENTO_TEST_RECORD": str(record_path),
            "AGENTO_TEST_SCAN": str(tmp_path),
            "AGENTO_TEST_MARKER": marker,
            "AGENTO_SSH_PRIVATE_KEY": key,
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        }
        proc = subprocess.run(
            _wrapped(["true"], tmp_path / "nohome" / ".ssh"),
            env=env, text=True, capture_output=True,
        )
        record = record_path.read_text()
        assert proc.returncode == 0
        assert marker in record  # the key really was delivered
        assert "FIFOS:0" in record  # no named pipe existed while it was
        leak = [ln for ln in record.splitlines() if ln.startswith("LEAK:")]
        assert leak == ["LEAK:"], leak  # and no file contained it

    def test_a_signal_to_the_agent_command_is_propagated(self, tmp_path):
        """The ssh-agent layer must not swallow the run's termination signal."""
        _stub_bin(tmp_path)
        proc, _ = _run(
            _wrapped(["bash", "-c", "kill -TERM $$"], tmp_path / "nohome" / ".ssh"),
            tmp_path, AGENTO_SSH_PRIVATE_KEY="KEYDATA",
        )
        assert proc.returncode in (-15, 143)

    def test_ttl_falls_back_to_43200_only_when_unset(self, tmp_path):
        _stub_bin(tmp_path)
        _, record = _run(
            _wrapped(["true"], tmp_path / "nohome" / ".ssh"), tmp_path,
            AGENTO_SSH_PRIVATE_KEY="KEYDATA",
        )
        assert "AGENT_ARGV:-t 43200" in record
