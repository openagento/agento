from __future__ import annotations

import json
import logging
import os
from pathlib import Path

LOG_DIR = "/app/logs"

_JSON_EXTRA_FIELDS = ("job_id", "reference_id", "type", "attempt", "status", "duration_ms", "result_summary")


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = f"[{self.formatTime(record, '%Y-%m-%d %H:%M:%S')}] [{record.levelname}] {record.getMessage()}"
        extras = {k: getattr(record, k) for k in _JSON_EXTRA_FIELDS if getattr(record, k, None) is not None}
        if extras:
            pairs = " ".join(f"{k}={v}" for k, v in extras.items())
            msg = f"{msg} | {pairs}"
        if record.exc_info:
            msg = f"{msg}\n{self.formatException(record.exc_info)}"
        return msg


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for the consumer."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _JSON_EXTRA_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
            entry["error_class"] = record.exc_info[1].__class__.__name__
        return json.dumps(entry, ensure_ascii=False)


# Module loggers whose records are wired to the configured log file.
#
# NOT the whole `agento` tree. Many sites across src/agento pass `exc_info=True`
# / call `logger.exception(...)`, and the formatter these handlers carry —
# `_Formatter` — writes the whole traceback verbatim via `formatException`;
# making all of them persistent at once, unaudited, is a credential leak waiting
# for the right exception. A namespace is added here only after its
# exception-carrying sites have been audited for secrets — every one of
# app_monitor's was, in Step 2a.
ATTACHED_NAMESPACES = ("agento.modules.app_monitor",)


def get_logger(name: str, log_file: str | None = None, *, stderr: bool = True) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = _Formatter()

    if stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    if log_file:
        path = Path(log_file)
        os.makedirs(path.parent, exist_ok=True)
        fh = logging.FileHandler(str(path))
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    if log_file:
        # Module code uses logging.getLogger(__name__) -> "agento.modules.…",
        # which has no handler of its own. Without this, a swallowed alert
        # failure reaches only logging.lastResort (bare stderr) and never the
        # structured log — which is exactly how app_monitor's SMTP failures
        # stayed invisible.
        #
        # Deliberately ONE module namespace, not "agento" and not the root.
        # `exc_info=True` / `logger.exception(...)` sites are spread across
        # src/agento (codex, outlook, jira, github, bitbucket, workspace_build,
        # config_resolver, bootstrap, …). Attaching a file handler to the whole
        # `agento` tree would make every one of those tracebacks persistent in
        # logs/consumer.log in a single change, and the handler's formatter
        # (`_Formatter`) writes the whole traceback verbatim — an unaudited
        # traceback is a credential leak waiting for the right exception.
        # Step 2a audited and sanitized every exception-carrying site in
        # app_monitor; only that subtree is wired up.
        # Widening this is a separate change with its own log-safety audit, and
        # ROADMAP.md carries it (Step 5).
        for namespace_name in ATTACHED_NAMESPACES:
            namespace = logging.getLogger(namespace_name)
            if not namespace.handlers:
                namespace.setLevel(logging.DEBUG)
                for handler in logger.handlers:
                    namespace.addHandler(handler)

    return logger
