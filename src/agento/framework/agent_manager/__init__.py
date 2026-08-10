"""Agent Manager — multi-credential orchestration for agent harnesses."""

from .auth import (
    AuthResult,
    authenticate_interactive,
    get_available_scopes,
    save_credentials,
)
from .config import AgentManagerConfig
from .credential_resolver import CredentialResolver
from .credential_store import (
    RefreshLease,
    clear_auto_credential_error,
    clear_credential_error,
    count_credentials_for_scope,
    deregister_credential,
    get_credential,
    lease_owner_for_job,
    list_credentials,
    mark_credential_error,
    register_credential,
    release_credential_lease,
    renew_credential_leases,
    select_credential,
    set_credential_priority,
    throttle_credential,
)
from .errors import AuthenticationError, CredentialLeasedError, UsageLimitError
from .models import (
    CredentiallessUsage,
    CredentialRecord,
    CredentialStatus,
    UsageSummary,
)
from .usage_store import (
    get_credentialless_usage,
    get_usage_summaries,
    get_usage_summary,
    record_usage,
)

__all__ = [
    "AgentManagerConfig",
    "AuthResult",
    "AuthenticationError",
    "CredentialLeasedError",
    "CredentialRecord",
    "CredentialResolver",
    "CredentialStatus",
    "CredentiallessUsage",
    "RefreshLease",
    "UsageLimitError",
    "UsageSummary",
    "authenticate_interactive",
    "clear_auto_credential_error",
    "clear_credential_error",
    "count_credentials_for_scope",
    "deregister_credential",
    "get_available_scopes",
    "get_credential",
    "get_credentialless_usage",
    "get_usage_summaries",
    "get_usage_summary",
    "lease_owner_for_job",
    "list_credentials",
    "mark_credential_error",
    "record_usage",
    "register_credential",
    "release_credential_lease",
    "renew_credential_leases",
    "save_credentials",
    "select_credential",
    "set_credential_priority",
    "throttle_credential",
]
