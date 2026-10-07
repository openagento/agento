"""Child process for Claude's ``check_model``: one GET, print the status code only.

Run as ``python -m agento.modules.claude.src.model_probe`` with ``{"url", "headers"}``
as JSON on stdin (the credential is in the headers: never argv, never env). It runs
in its own process because no in-process HTTP client stops at a hard deadline (DNS,
connect and header reads each have their own timeout); the parent's
``subprocess.run(timeout=…)`` kills and reaps this process instead. The body is never
read. On any error it prints nothing and exits 1.
"""
from __future__ import annotations

import json
import sys

import httpx


def probe(url: str, headers: dict[str, str], timeout: float = 30.0) -> int:
    with httpx.Client(timeout=timeout) as client, client.stream("GET", url, headers=headers) as resp:
        return resp.status_code


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        print(probe(request["url"], request["headers"]))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
