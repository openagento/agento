"""Two-arm dispatch + the local-arm sanitizer."""
from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from agento.framework.config_test import ERROR, FAIL, NOT_CONFIGURED, OK, TestResult
from agento.framework.config_test.runner import run_config_test, sanitize

MODULE = "agento.framework.config_test.runner"


# --- sanitize ---------------------------------------------------------------

def test_sanitize_masks_a_value_that_leaked_into_the_message():
    msg = sanitize("login rejected for hunter2xyz", {"m/p": "hunter2xyz"})
    assert "hunter2xyz" not in msg
    assert "***" in msg


def test_sanitize_ignores_short_and_empty_non_obscure_values():
    # A 3-char non-secret would mask far too much (e.g. "587" in a port message).
    assert sanitize("port 587 refused", {"m/port": "587", "m/u": ""}) == "port 587 refused"


def test_sanitize_masks_a_short_value_when_its_field_is_obscure():
    """The length floor is a readability tradeoff for non-secrets only. A value
    that came out of an `obscure` field is masked whatever its length — a 3-char
    password must not leak because it was short."""
    out = sanitize("rejected pw abc", {"m/p": "abc"}, obscure_paths={"m/p"})
    assert "abc" not in out
    assert "***" in out


def test_sanitize_flattens_and_strips():
    assert sanitize("  two\nlines  ", {}) == "two lines"


def test_sanitize_leaves_a_clean_message_untouched():
    assert sanitize("authentication failed (535)", {"m/p": "s3cret-value"}) == (
        "authentication failed (535)"
    )


# --- dispatch ---------------------------------------------------------------

def _module(root: Path, name: str, system) -> Path:
    d = root / "app" / "code" / name
    d.mkdir(parents=True)
    (d / "module.json").write_text(json.dumps({"name": name, "version": "0.1.0"}))
    (d / "system.json").write_text(json.dumps(system))
    return d


def test_a_field_with_no_tester_is_an_error(tmp_path):
    _module(tmp_path, "m", {"f": {"type": "string"}})
    r = run_config_test(None, "m/f", scope="default", project_root=tmp_path)
    assert (r.status, r.code) == (ERROR, "NO_TESTER")


def test_a_toolbox_kind_is_delegated_verbatim(tmp_path, monkeypatch):
    _module(tmp_path, "m", {"f": {"type": "obscure", "tester": {"kind": "smtp", "host": "{m/h}"}}})
    seen = {}

    def _delegate(conn, path, *, scope, scope_id=0):
        seen.update(path=path, scope=scope, scope_id=scope_id)
        return TestResult(OK, "ok (12 ms)", code="OK")

    monkeypatch.setattr(f"{MODULE}.run_toolbox_test", _delegate)
    r = run_config_test(None, "m/f", scope="agent_view", scope_id=7, project_root=tmp_path)
    assert r.status == OK
    assert seen == {"path": "m/f", "scope": "agent_view", "scope_id": 7}


def test_a_named_tester_is_delegated_the_same_way(tmp_path, monkeypatch):
    _module(tmp_path, "m", {"f": {"type": "obscure", "tester": "named_probe"}})
    monkeypatch.setattr(
        f"{MODULE}.run_toolbox_test",
        lambda conn, path, **kw: TestResult(OK, "ok", code="OK"),
    )
    assert run_config_test(None, "m/f", scope="default", project_root=tmp_path).status == OK


class _Local:
    """A local tester: it resolves its own config, so the runner hands it none."""

    result = TestResult(OK, "pair ok", code="OK")
    seen: ClassVar[dict] = {}

    def run(self, conn, *, scope, scope_id):
        type(self).seen = {"conn": conn, "scope": scope, "scope_id": scope_id}
        return self.result


def _local_module(tmp_path):
    return _module(tmp_path, "m", {
        "f": {"type": "obscure", "tester": {"kind": "local", "class": "src.testers.T"}},
    })


def test_a_local_tester_is_imported_from_its_own_module_dir(tmp_path, monkeypatch):
    module_dir = _local_module(tmp_path)
    seen = {}

    def _import(dir_, class_path):
        seen.update(dir=dir_, class_path=class_path)
        return _Local

    monkeypatch.setattr(f"{MODULE}.import_class", _import)
    r = run_config_test(None, "m/f", scope="agent_view", scope_id=7, project_root=tmp_path)
    assert r.status == OK
    assert seen == {"dir": module_dir, "class_path": "src.testers.T"}
    assert _Local.seen == {"conn": None, "scope": "agent_view", "scope_id": 7}


def test_an_unimportable_local_class_is_an_error(tmp_path, monkeypatch):
    _local_module(tmp_path)
    monkeypatch.setattr(
        f"{MODULE}.import_class",
        lambda d, c: (_ for _ in ()).throw(ImportError("no module named testers")),
    )
    r = run_config_test(None, "m/f", scope="default", project_root=tmp_path)
    assert (r.status, r.code) == (ERROR, "TESTER_UNAVAILABLE")


def test_a_local_tester_that_raises_is_an_error_not_a_crash(tmp_path, monkeypatch):
    _local_module(tmp_path)

    class _Boom:
        def run(self, conn, *, scope, scope_id):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(f"{MODULE}.import_class", lambda d, c: _Boom)
    r = run_config_test(None, "m/f", scope="default", project_root=tmp_path)
    assert (r.status, r.code) == (ERROR, "TESTER_RAISED")
    assert "kaboom" not in r.message      # an exception message is not a diagnostic
    assert "RuntimeError" in r.message


def test_a_local_tester_returning_nonsense_is_an_error(tmp_path, monkeypatch):
    _local_module(tmp_path)

    class _Wrong:
        def run(self, conn, *, scope, scope_id):
            return "fine, I guess"

    monkeypatch.setattr(f"{MODULE}.import_class", lambda d, c: _Wrong)
    r = run_config_test(None, "m/f", scope="default", project_root=tmp_path)
    assert (r.status, r.code) == (ERROR, "BAD_RESULT")


def test_a_local_tester_returning_an_unknown_status_is_an_error(tmp_path, monkeypatch):
    _local_module(tmp_path)

    class _Wrong:
        def run(self, conn, *, scope, scope_id):
            return TestResult("weird", "hm")

    monkeypatch.setattr(f"{MODULE}.import_class", lambda d, c: _Wrong)
    r = run_config_test(None, "m/f", scope="default", project_root=tmp_path)
    assert (r.status, r.code) == (ERROR, "BAD_RESULT")


def test_a_local_result_has_its_code_shape_enforced(tmp_path, monkeypatch):
    _local_module(tmp_path)

    class _Chatty:
        def run(self, conn, *, scope, scope_id):
            return TestResult(FAIL, "rejected", code="not a code: 535")

    monkeypatch.setattr(f"{MODULE}.import_class", lambda d, c: _Chatty)
    r = run_config_test(None, "m/f", scope="default", project_root=tmp_path)
    assert (r.status, r.code) == (FAIL, "")


def test_a_local_message_is_flattened(tmp_path, monkeypatch):
    _local_module(tmp_path)

    class _Multi:
        def run(self, conn, *, scope, scope_id):
            return TestResult(NOT_CONFIGURED, "one\ntwo")

    monkeypatch.setattr(f"{MODULE}.import_class", lambda d, c: _Multi)
    r = run_config_test(None, "m/f", scope="default", project_root=tmp_path)
    assert r.status == NOT_CONFIGURED
    assert "\n" not in r.message


class TestScopeValidation:
    """`(scope, scope_id)` is checked once, for BOTH arms, before anything runs.

    A local tester builds a `ScopedConfigService`, which resolves an unknown
    scope and a zero id alike as a default-scope read — so without this the
    verdict could be about the default credential while the caller named a view.
    """

    def test_a_zero_id_is_refused_for_a_scope_that_needs_one(self):
        for scope in ("workspace", "agent_view"):
            r = run_config_test(None, "m/f", scope=scope, scope_id=0)
            assert (r.status, r.code) == (ERROR, "SCOPE_ID_INVALID"), scope

    def test_a_negative_id_is_refused(self):
        r = run_config_test(None, "m/f", scope="agent_view", scope_id=-1)
        assert r.code == "SCOPE_ID_INVALID"

    def test_the_default_scope_takes_no_id(self):
        r = run_config_test(None, "m/f", scope="default", scope_id=3)
        assert r.code == "SCOPE_ID_INVALID"

    def test_an_unknown_scope_is_refused(self):
        r = run_config_test(None, "m/f", scope="tenant", scope_id=1)
        assert (r.status, r.code) == (ERROR, "SCOPE_UNSUPPORTED")

    def test_a_valid_pair_gets_past_the_gate(self):
        # NO_TESTER means the scope check passed and the declaration lookup ran.
        r = run_config_test(None, "nosuchmodule/f", scope="default", scope_id=0)
        assert r.code == "NO_TESTER"


class TestAMalformedResultNeverRaises:
    """`run_config_test` is documented never to raise. A tester is module code
    and the toolbox body is wire data, so every field of both is input: a type
    check has to come before any regex or dict lookup."""

    def test_a_non_string_code_from_a_local_tester_is_dropped(self):
        from agento.framework.config_test.runner import _clean

        cleaned = _clean(TestResult(OK, "fine", code=7))       # CODE_RE.match(7) raised
        assert (cleaned.status, cleaned.code) == (OK, "")

    def test_an_unhashable_status_on_the_wire_is_a_bad_body(self, monkeypatch):
        import httpx
        import respx

        from agento.framework.config_test.toolbox import run_toolbox_test

        monkeypatch.setattr(
            "agento.framework.config_test.toolbox._resolve_toolbox_url",
            lambda conn: "http://toolbox:3001",
        )
        with respx.mock:
            respx.post("http://toolbox:3001/config-test").mock(
                return_value=httpx.Response(200, json={"status": [], "code": "OK"})
            )
            r = run_toolbox_test(None, "m/f", scope="default")
        assert (r.status, r.code) == (ERROR, "TOOLBOX_BAD_BODY")
