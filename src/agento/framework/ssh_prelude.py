"""Spawn wrapper: the agent gets a signing capability, never a key.

The old prelude symlinked the per-run ``$HOME/.ssh`` into ``/root/.ssh`` and
``/home/agent/.ssh``, because OpenSSH resolves ``~/.ssh`` through
``getpwuid(getuid())->pw_dir`` and not ``$HOME``. Two problems: the key was a file on a
shared mount every agent_view could read, and concurrent runs of DIFFERENT views raced on
that one shared path.

Now the private key arrives in ``AGENTO_SSH_PRIVATE_KEY`` and is handed to ``ssh-add``
through a **process substitution** on fd 3 — where ``/dev/fd`` exists (Linux, both our
images) bash backs ``<(...)`` with an anonymous pipe, so the key never becomes a
filesystem object: no temp file, no FIFO, no cleanup path that can fail, nothing a signal
or a timeout can leave behind. ``printf`` is a bash builtin, so it never reaches any
process's argv either. The agent command is then ``exec``ed with the key removed from its
environment. ``SSH_AUTH_SOCK`` is the only SECRET-BEARING thing it holds; the non-secret
``GIT_SSH_COMMAND``, ``AGENTO_SSH_TTL`` and the agent's own ``SSH_AGENT_PID`` stay in that
environment too — none of them is a credential, and ``AGENTO_SSH_PRIVATE_KEY`` is the one
name ``env -u`` removes.

This is NOT isolation between agent_views — under one uid the environ, fd 3 and the agent
socket are all readable/usable by a same-uid peer while they exist. See ``DECISIONS.md``.
"""
from __future__ import annotations

# The passwd home `ssh` would consult regardless of $HOME. A module constant rather than
# a literal so behavioural tests can point the generated script at a temp dir instead of
# running `rm -rf` against the developer's own /home/agent.
PASSWD_HOME_SSH_DIR = "/home/agent/.ssh"

_PRELUDE_TEMPLATE = r"""
# Any default identity a previous run planted in the passwd home must not survive into
# this one: ssh expands ~/.ssh via getpwuid(), not $HOME, so a planted id_rsa there would
# be picked up by a run that was granted no key. FAIL CLOSED: a planted identity we
# cannot remove is an active isolation break, not a warning. /root/.ssh is NOT reachable
# by uid agent (nor is /root traversable), so the root container entrypoints own that path.
rm -rf @@PASSWD_SSH@@ 2>/dev/null
if [ -e @@PASSWD_SSH@@ ] || [ -L @@PASSWD_SSH@@ ]; then
  echo "agento: @@PASSWD_SSH@@ exists and could not be removed - refusing to run" >&2
  exit 78
fi

# Ordering matters: bash creates a process substitution during EXPANSION, so by the time
# any check inside ssh-agent runs, a FIFO-backed substitution would already exist WITH
# THE KEY IN IT. So preflight the mechanism with a NON-SECRET payload first, and only
# then build the key-bearing one.
substitution_is_pipe() (
  exec 4< <(printf 'probe\n') 2>/dev/null || return 1
  case "$(readlink /proc/self/fd/4 2>/dev/null)" in pipe:*) return 0 ;; *) return 1 ;; esac
)

if [ -n "${AGENTO_SSH_PRIVATE_KEY:-}" ] && substitution_is_pipe; then
  # fd 3 is a PROCESS SUBSTITUTION. Where /dev/fd exists - Linux, both our images - bash
  # backs <(...) with an anonymous pipe, so there is no size threshold and no temp-file
  # fallback. On a platform WITHOUT /dev/fd bash would use a named FIFO instead, which is
  # a filesystem object - the preflight above already refused that case before this line
  # could run; the inner readlink repeats it as defence in depth, and the image build
  # asserts the same property with a non-secret payload.
  # `printf` is a bash builtin, so the key never appears in any process's argv.
  exec env -u AGENTO_SSH_PRIVATE_KEY ssh-agent -t "${AGENTO_SSH_TTL:-43200}" \
    bash -c 'case "$(readlink /proc/self/fd/3 2>/dev/null)" in
               pipe:*) ssh-add -q - <&3 2>/dev/null && { exec 3<&-; exec "$@"; }
                       echo "agento: ssh-add failed - this run has no SSH identity" >&2 ;;
               *)      echo "agento: fd 3 is not an anonymous pipe - refusing to load the key" >&2 ;;
             esac
             exec 3<&-
             exec env -u SSH_AUTH_SOCK "$@"' agento-ssh-prelude "$@" 3< <(printf '%s\n' "$AGENTO_SSH_PRIVATE_KEY")
fi
if [ -n "${AGENTO_SSH_PRIVATE_KEY:-}" ]; then
  echo "agento: process substitution is not pipe-backed here - refusing to deliver the SSH key" >&2
fi
unset SSH_AUTH_SOCK
exec env -u AGENTO_SSH_PRIVATE_KEY "$@"
"""


def ssh_prelude_script(passwd_home_ssh_dir: str = PASSWD_HOME_SSH_DIR) -> str:
    """The wrapper's shell source. Separate from ``wrap_with_ssh_prelude`` so tests can
    assert on the text (and on the ORDER of its parts) without spawning anything."""
    return _PRELUDE_TEMPLATE.replace("@@PASSWD_SSH@@", passwd_home_ssh_dir)


def wrap_with_ssh_prelude(
    cmd: list[str],
    *,
    passwd_home_ssh_dir: str = PASSWD_HOME_SSH_DIR,
) -> list[str]:
    """Wrap ``cmd`` so it runs under a per-run ``ssh-agent`` holding the configured key.

    ``bash`` (not ``sh``) because of the process substitution. Exit status is forwarded
    through the ``ssh-agent`` layer, and the command's **stdin is untouched** — the key
    rides fd 3, which matters because the consumer passes ``stdin=DEVNULL`` and the host
    path attaches a TTY.
    """
    return [
        "bash", "-c", ssh_prelude_script(passwd_home_ssh_dir),
        "agento-ssh-prelude", *cmd,
    ]
