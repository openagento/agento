"""A module logger's records must reach the configured log file."""
from __future__ import annotations

import logging

import pytest

from agento.framework.log import ATTACHED_NAMESPACES, get_logger

# The one attached namespace, plus a sibling that must stay unattached.
ATTACHED = "agento.modules.app_monitor.src.observers"
NOT_ATTACHED = "agento.modules.jira.src.channel"
TOUCHED = ("consumer", "setup", "agento", "agento.modules.app_monitor",
           "agento.modules.jira", ATTACHED, NOT_ATTACHED)


@pytest.fixture(autouse=True)
def clean_loggers():
    """get_logger is idempotent by design (it returns early if handlers exist),
    so each test must start from a clean slate."""
    def reset():
        for name in TOUCHED:
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                lg.removeHandler(h)
                h.close()
            lg.setLevel(logging.NOTSET)
    reset()
    yield
    reset()


def test_the_attached_set_is_exactly_the_audited_namespace():
    """A namespace joins this tuple only after its exception-carrying sites have
    been audited for credentials (Step 2a did all of app_monitor's). Widening it is
    a deliberate change with an audit attached, not an edit."""
    assert ATTACHED_NAMESPACES == ("agento.modules.app_monitor",)


def test_a_module_logger_warning_reaches_the_log_file(tmp_path):
    log_file = tmp_path / "consumer.log"
    get_logger("consumer", str(log_file), stderr=False)

    logging.getLogger(ATTACHED).warning("SMTP send failed")

    assert log_file.is_file()
    assert "SMTP send failed" in log_file.read_text()


def test_exc_info_from_a_module_logger_is_recorded(tmp_path):
    log_file = tmp_path / "consumer.log"
    get_logger("consumer", str(log_file), stderr=False)

    try:
        raise ValueError("535 authentication failed")
    except ValueError:
        logging.getLogger(ATTACHED).warning("alert failed", exc_info=True)

    body = log_file.read_text()
    assert "alert failed" in body
    assert "535 authentication failed" in body


def test_the_named_logger_still_works(tmp_path):
    log_file = tmp_path / "consumer.log"
    logger = get_logger("consumer", str(log_file), stderr=False)
    logger.info("hello from the consumer")
    assert "hello from the consumer" in log_file.read_text()


def test_a_logger_without_a_log_file_does_not_configure_the_namespace(tmp_path):
    get_logger("setup", stderr=False)
    assert logging.getLogger("agento.modules.app_monitor").handlers == []


def test_calling_twice_does_not_duplicate_records(tmp_path):
    log_file = tmp_path / "consumer.log"
    get_logger("consumer", str(log_file), stderr=False)
    get_logger("consumer", str(log_file), stderr=False)
    logging.getLogger(ATTACHED).warning("once")
    assert log_file.read_text().count("once") == 1


def test_the_agento_tree_is_not_attached(tmp_path):
    """The other `exc_info=True` sites stay on lastResort until each subtree has
    had its own log-safety audit — see ROADMAP.md."""
    log_file = tmp_path / "consumer.log"
    get_logger("consumer", str(log_file), stderr=False)
    assert logging.getLogger("agento").handlers == []
    logging.getLogger(NOT_ATTACHED).warning("unaudited traceback")
    assert "unaudited traceback" not in log_file.read_text()


def test_a_non_agento_logger_is_unaffected(tmp_path):
    log_file = tmp_path / "consumer.log"
    get_logger("consumer", str(log_file), stderr=False)
    logging.getLogger("some.third.party").warning("not ours")
    assert "not ours" not in log_file.read_text()


def test_no_attached_namespace_logs_raw_exception_text():
    """The class guard for the attachment above.

    Attaching file handlers to a namespace makes every `exc_info=True` /
    `logger.exception(...)` in it persist the exception's own message — text this
    project cannot enumerate, because it comes from a DB driver, an SMTP server
    or an agent transcript. A namespace may only be attached when it has none:
    the audited sites log the exception TYPE and, where the message is needed,
    scrub it first (`observers._log_alert_failure` via `config_test.sanitize`).

    Structural, not textual: an AST walk over every source file of every attached
    namespace, so a new site added anywhere in the subtree fails here.
    """
    import ast
    import pathlib

    offenders = []
    for namespace in ATTACHED_NAMESPACES:
        root = pathlib.Path(*namespace.split("."))
        base = pathlib.Path("src") / root
        assert base.is_dir(), base
        for source in base.rglob("*.py"):
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if any(k.arg == "exc_info" for k in node.keywords):
                    offenders.append(f"{source}:{node.lineno} exc_info")
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr == "exception"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in ("logger", "log", "logging")):
                    offenders.append(f"{source}:{node.lineno} logger.exception")
    assert offenders == [], offenders
