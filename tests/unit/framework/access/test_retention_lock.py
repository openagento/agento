"""The retention lock web's create_launch shares with the toolbox prune (PRD E6 §5)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework.access import launches

FIXTURE = json.loads((Path(__file__).parents[3] / "fixtures" / "retention_lock_v1.json").read_text())


@pytest.mark.parametrize(("code", "name"), sorted(FIXTURE["cases"].items()))
def test_the_lock_name_matches_the_toolbox(code, name):
    assert launches.retention_lock_name(code) == name
    assert len(name) <= 64  # MySQL's named-lock limit


def _conn(got):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"got": got}
    return conn, cur


def test_acquire_and_release_on_one_connection():
    conn, cur = _conn(1)
    with launches.retention_lock(conn, "site"):
        pass
    sql = [c.args for c in cur.execute.call_args_list]
    assert sql == [("SELECT GET_LOCK(%s, %s) AS got", (FIXTURE["cases"]["site"], 5)),
                   ("SELECT RELEASE_LOCK(%s)", (FIXTURE["cases"]["site"],))]


def test_released_when_the_body_raises():
    conn, cur = _conn(1)
    with pytest.raises(ValueError), launches.retention_lock(conn, "site"):
        raise ValueError
    assert cur.execute.call_args.args[0] == "SELECT RELEASE_LOCK(%s)"


@pytest.mark.parametrize("got", [0, None])
def test_busy_or_error_raises_and_releases_nothing(got):
    conn, cur = _conn(got)
    with pytest.raises(launches.RetentionBusy), launches.retention_lock(conn, "site"):
        pytest.fail("the body must not run")
    assert cur.execute.call_count == 1
