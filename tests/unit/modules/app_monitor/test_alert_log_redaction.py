"""An alert-failure log line must not carry the SMTP password."""
from __future__ import annotations

import logging

from agento.modules.app_monitor.src.emailer import SmtpConfig
from agento.modules.app_monitor.src.observers import _log_alert_failure


def _smtp(password="hunter2xyz"):
    # `_smtp_config()` returns the SmtpConfig dataclass from emailer.py — NOT a
    # dict. A helper reaching for `.get("smtp_password")` raises AttributeError
    # inside an `except` block, i.e. it turns a swallowed send failure into a
    # crash in the observer.
    return SmtpConfig(
        host="smtp.example.com", port=587, user="alerts@example.com",
        password=password, from_addr="alerts@example.com", tls=True,
    )


def test_the_password_is_masked_in_the_message():
    err = Exception("535 5.7.8 authentication failed for pass hunter2xyz")
    assert "hunter2xyz" not in _log_alert_failure_text(_smtp(), err)


def test_the_reply_code_survives_the_masking():
    # The operator's actual question is "which failure was it" — 535 answers it.
    text = _log_alert_failure_text(_smtp(), Exception("(535, '5.7.8 authentication failed')"))
    assert "535" in text
    assert "Exception" in text


def test_an_empty_password_masks_nothing_and_still_logs():
    # `_smtp_config()` defaults `password` to "" when unset; an empty needle must
    # not turn the whole message into mask characters.
    assert "boom" in _log_alert_failure_text(_smtp(password=""), Exception("boom"))


def test_a_none_config_is_not_an_error():
    # Defensive: `smtp` is non-None at every call site today, but a helper that
    # raises inside `except` is the failure mode this whole step exists to remove.
    assert "boom" in _log_alert_failure_text(None, Exception("boom"))


def _log_alert_failure_text(smtp, err, caplog=None):
    logger = logging.getLogger("agento.modules.app_monitor.src.observers")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        _log_alert_failure(logger, smtp, err, "job_id=7")
    finally:
        logger.removeHandler(handler)
    assert len(records) == 1
    assert records[0].exc_info is None      # the traceback text is the leak
    return records[0].getMessage()
