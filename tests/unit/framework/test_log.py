from __future__ import annotations

import json
import logging

from agento.framework.log import JsonFormatter, _Formatter, get_logger


def _make_record(msg="test message", level=logging.INFO, **extras):
    logger = logging.getLogger("test.json")
    record = logger.makeRecord(
        name="test.json",
        level=level,
        fn="test_log.py",
        lno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extras.items():
        setattr(record, k, v)
    return record


def test_json_formatter_basic():
    fmt = JsonFormatter()
    record = _make_record("hello world")
    output = fmt.format(record)

    data = json.loads(output)
    assert data["msg"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "test.json"
    assert "ts" in data


def test_json_formatter_extra_fields():
    fmt = JsonFormatter()
    record = _make_record("job started", job_id=42, reference_id="AI-1")
    output = fmt.format(record)

    data = json.loads(output)
    assert data["job_id"] == 42
    assert data["reference_id"] == "AI-1"


def test_json_formatter_exception():
    fmt = JsonFormatter()
    logger = logging.getLogger("test.exc")
    try:
        raise ValueError("bad value")
    except ValueError:
        import sys

        record = logger.makeRecord(
            name="test.exc",
            level=logging.ERROR,
            fn="test_log.py",
            lno=1,
            msg="job failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    output = fmt.format(record)
    data = json.loads(output)
    assert data["error"] == "bad value"
    assert data["error_class"] == "ValueError"


def test_json_formatter_missing_extras_omitted():
    fmt = JsonFormatter()
    record = _make_record("plain message")
    output = fmt.format(record)

    data = json.loads(output)
    assert "job_id" not in data
    assert "reference_id" not in data
    assert "type" not in data
    assert "duration_ms" not in data


def test_json_formatter_emits_arbitrary_extras():
    # Extras outside the historical priority tuple must survive, not be silently dropped.
    fmt = JsonFormatter()
    record = _make_record(
        "sender not in allowed_senders",
        message_id="AAMkAG...trunc",
        sender_domain="example.com",
    )
    data = json.loads(fmt.format(record))
    assert data["message_id"] == "AAMkAG...trunc"
    assert data["sender_domain"] == "example.com"


def test_json_formatter_priority_fields_ordered_first():
    fmt = JsonFormatter()
    record = _make_record("policy", allowed_senders_count=3, job_id=7)
    data = json.loads(fmt.format(record))
    keys = list(data.keys())
    # job_id (a priority field) precedes the ad-hoc extra regardless of set order.
    assert keys.index("job_id") < keys.index("allowed_senders_count")


def test_text_formatter_emits_arbitrary_extras():
    fmt = _Formatter()
    record = _make_record(
        "Effective outlook policy",
        agent_view="av1",
        mailbox="ops",
        mode="poll",
        allowed_senders_count=4,
    )
    out = fmt.format(record)
    assert "agent_view=av1" in out
    assert "mailbox=ops" in out
    assert "mode=poll" in out
    assert "allowed_senders_count=4" in out


def test_text_formatter_no_extras_has_no_separator():
    fmt = _Formatter()
    record = _make_record("plain message")
    out = fmt.format(record)
    assert " | " not in out


def test_extras_ignore_reserved_record_attributes():
    # Standard LogRecord attributes must never leak into the extras section.
    fmt = _Formatter()
    record = _make_record("hello")
    out = fmt.format(record)
    assert "process=" not in out
    assert "levelname=" not in out
    assert "funcName=" not in out


def test_get_logger_no_stderr(tmp_path):
    log_file = str(tmp_path / "test.log")
    name = "test-no-stderr"
    logger = logging.getLogger(name)
    logger.handlers.clear()

    result = get_logger(name, log_file, stderr=False)
    assert len(result.handlers) == 1
    assert isinstance(result.handlers[0], logging.FileHandler)
    result.handlers[0].close()
    logger.handlers.clear()


def test_get_logger_default_has_stderr():
    name = "test-default-stderr"
    logger = logging.getLogger(name)
    logger.handlers.clear()

    result = get_logger(name)
    assert len(result.handlers) == 1
    assert type(result.handlers[0]) is logging.StreamHandler
    logger.handlers.clear()
