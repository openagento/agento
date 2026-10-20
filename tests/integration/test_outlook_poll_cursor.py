"""Integration: the per-mailbox delta cursor store round-trips against real MySQL."""
from __future__ import annotations

from agento.modules.outlook.src.cursor import load_cursors, save_cursor

from .conftest import _test_connection


def test_save_then_load_round_trips_keyed_by_normalized_upn():
    link = "https://graph.microsoft.com/v1.0/users/agent@example.com/mailFolders/Inbox/messages/delta?$deltatoken=AAA"
    conn = _test_connection(autocommit=False)
    try:
        save_cursor(conn, "  Agent@Example.COM ", link)
        cursors = load_cursors(conn)
    finally:
        conn.close()
    assert cursors == {"agent@example.com": link}


def test_save_is_an_upsert_on_mailbox():
    conn = _test_connection(autocommit=False)
    try:
        save_cursor(conn, "a@x.com", "link1")
        save_cursor(conn, "a@x.com", "link2")
        cursors = load_cursors(conn)
    finally:
        conn.close()
    assert cursors == {"a@x.com": "link2"}  # one row, latest wins


def test_load_empty_is_empty_dict():
    conn = _test_connection(autocommit=False)
    try:
        assert load_cursors(conn) == {}
    finally:
        conn.close()
