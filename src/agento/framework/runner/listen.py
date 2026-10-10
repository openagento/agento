"""Socket activation for the runner. Runs as root, as a script (no package import, no
module code, no dispatch: EVT-3), before the server drops to ``agent``.

It makes the socket directory ``root:root 0755``, unlinks a stale socket, binds and
listens (mode 0666, so the worker can connect), and execs the server as ``agent`` with
the listener on fd 3. ``agent`` cannot unlink, rename or bind anything in that directory,
so only this container's server answers on the name until the container stops.
"""
import contextlib
import os
import socket
import sys

path = os.environ["AGENTO_RUNNER_SOCKET"]
directory = os.path.dirname(path)
os.makedirs(directory, exist_ok=True)
os.chown(directory, 0, 0)
os.chmod(directory, 0o755)
with contextlib.suppress(FileNotFoundError):
    os.unlink(path)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(path)
os.chmod(path, 0o666)
listener.listen(128)
os.dup2(listener.fileno(), 3)
os.set_inheritable(3, True)  # dup2 onto itself keeps it close-on-exec
os.environ["LISTEN_FDS"] = "1"
os.execvp("gosu", ["gosu", "agent", sys.executable, "-m", "agento.framework.runner.server"])
