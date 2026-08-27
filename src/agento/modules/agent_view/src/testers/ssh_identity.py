"""Config tester: does the stored SSH identity actually work?

Answers the question the old ``sha256-tag`` fingerprint could not: the key
parses, and it is the private half of the stored public key.

This is a ``local`` tester — it runs in the framework process and resolves its
own two config paths, which is the same thing ``commands/identity_show.py`` has
always done. The framework hands it nothing but a connection and a scope, so
declaring it grants this module no capability its commands did not already have.
See docs/config/testers.md.
"""
from __future__ import annotations

from agento.framework.config_resolver import DecryptError, ScopedConfigService
from agento.framework.config_test import (
    ERROR,
    FAIL,
    NOT_CONFIGURED,
    OK,
    TestResult,
)
from agento.framework.ssh_keys import CODE_NOT_SET, CODE_OK, check_keypair

PRIVATE_PATH = "agent_view/identity/ssh_private_key"
PUBLIC_PATH = "agent_view/identity/ssh_public_key"


def check_identity(private: str | None, public: str | None) -> TestResult:
    """Map a :func:`check_keypair` result onto a ``TestResult``. No IO."""
    check = check_keypair(private, public)
    if check.code == CODE_OK:
        status = OK
    elif check.code == CODE_NOT_SET:
        status = NOT_CONFIGURED
    else:
        status = FAIL
    return TestResult(status, check.detail, code=check.code)


class SshIdentityTester:
    def run(self, conn, *, scope: str, scope_id: int) -> TestResult:
        try:
            svc = ScopedConfigService(conn, scope, scope_id)
            # strict=True on the private key only: an encrypted row that will not
            # decrypt must not fall through to config.json and report NOT_SET —
            # that silence is how the incident stayed invisible for four builds.
            # The public key is stored in clear, so the lenient read is correct.
            private = svc.get(PRIVATE_PATH, strict=True)
            public = svc.get(PUBLIC_PATH)
        except DecryptError:
            return TestResult(
                ERROR,
                "the stored private key could not be decrypted — is "
                "AGENTO_ENCRYPTION_KEY the one it was stored with?",
                code="DECRYPT_FAILED",
            )
        except Exception as e:
            # Type name only: a resolver exception message can quote the value it
            # was handling, and this string is printed in a TUI toast.
            return TestResult(
                ERROR,
                f"could not read the stored identity ({type(e).__name__})",
                code="READ_FAILED",
            )
        return check_identity(private, public)
