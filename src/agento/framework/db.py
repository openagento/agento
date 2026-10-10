from __future__ import annotations

import queue
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress

import pymysql
from pymysql.cursors import DictCursor

# Idle connections kept per config. The consumer sets it to max_workers (SCL-1).
IDLE_CAP = 10
_pools: dict[tuple, queue.Queue] = {}
_pools_lock = threading.Lock()
# True in a process that holds no database credential (the runner): a connect fails at
# once instead of waiting out connect_timeout on a host it cannot reach.
DISABLED = False


def get_connection(config: object) -> pymysql.Connection:
    """Create a single MySQL connection. Caller must close it."""
    if DISABLED:
        raise RuntimeError("this process has no database")
    return pymysql.connect(
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
        database=config.mysql_database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        init_command="SET time_zone = '+00:00'",
    )


@contextmanager
def pooled(config: object, connect: Callable[[object], pymysql.Connection] | None = None,
           isolation: str | None = None) -> Iterator[pymysql.Connection]:
    """Borrow a connection from the idle pool of ``config``; give it back on exit.

    Checkout never blocks: with nothing idle it opens a new one through ``connect``
    (default ``get_connection``; a caller passes its own module's name so a test patch on
    it still applies). The connection is pinged at checkout and rolled back at return, and
    one that raised is closed, never pooled. Never use it where ``GET_LOCK`` or
    ``SET SESSION`` runs: both outlive the checkout (a test guards this).

    ``isolation`` binds the level for the WHOLE checkout, however many transactions the
    borrower commits inside it - ``SET TRANSACTION`` alone would bind only the NEXT one and
    silently revert at the first commit, which is the opposite of what a caller asking for
    a level means. It is therefore a SESSION setting that the pool OWNS: applied in the
    same guarded region as the checkout ping (a pooled connection too dead to take it is
    discarded and replaced, so a borrow never fails for a reason a reconnect fixes) and
    restored from the global default at return, before the connection goes back in. That
    is what keeps the rule above intact - nothing outlives the checkout, because the pool
    undoes it rather than forbidding it.
    """
    connect = connect or get_connection
    key = (connect, config.mysql_host, config.mysql_port, config.mysql_user,
           config.mysql_password, config.mysql_database)
    with _pools_lock:
        idle = _pools.get(key)
        if idle is None:
            idle = _pools[key] = queue.Queue(maxsize=IDLE_CAP)

    def _prepare(c: pymysql.Connection) -> None:
        if isolation is None:
            return
        with c.cursor() as cur:
            cur.execute(f"SET SESSION TRANSACTION ISOLATION LEVEL {isolation}")

    conn = None
    try:
        conn = idle.get_nowait()
        conn.ping(reconnect=True)
        _prepare(conn)
    except queue.Empty:
        pass
    except Exception:
        _discard(conn)
        conn = None
    if conn is None:
        conn = connect(config)
        try:
            _prepare(conn)
        except BaseException:
            # Before the yield, so the cleanup below has not armed yet: a connection
            # opened here and left unprepared would be neither closed nor pooled.
            _discard(conn)
            raise
    try:
        yield conn
    except BaseException:
        _discard(conn)
        raise
    try:
        conn.rollback()
        if isolation is not None:
            with conn.cursor() as cur:
                cur.execute("SET SESSION transaction_isolation = @@GLOBAL.transaction_isolation")
        idle.put_nowait(conn)
    except Exception:  # pool full, or the rollback found the connection dead
        _discard(conn)


def _discard(conn: pymysql.Connection) -> None:
    # Already closed is fine: the error that brought us here is the one that matters.
    with suppress(Exception):
        conn.close()


def close_idle() -> None:
    """Close every idle pooled connection (tests; the consumer at shutdown)."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for idle in pools:
        while not idle.empty():
            _discard(idle.get_nowait())


def get_connection_or_exit(config: object) -> pymysql.Connection:
    """Like get_connection(), but prints a friendly error and exits on failure."""
    try:
        return get_connection(config)
    except pymysql.err.OperationalError:
        host = getattr(config, "mysql_host", "?")
        port = getattr(config, "mysql_port", "?")
        print(
            f"Error: Cannot connect to MySQL at {host}:{port}\n"
            "\n"
            "If running locally (outside Docker), set connection params:\n"
            "  export MYSQL_HOST=127.0.0.1\n"
            "  export MYSQL_PASSWORD=cronagent_pass\n"
            "\n"
            "Or add them to docker/.env or secrets.env.\n"
            "\n"
            "If running in Docker:\n"
            "  docker compose exec cron agento <command>",
            file=sys.stderr,
        )
        sys.exit(1)
