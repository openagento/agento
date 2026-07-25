from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OutlookConfig:
    """Python-side Outlook config — NO Graph secrets.

    The Graph credentials (``outlook_tenant_id``, ``outlook_client_id``, ``outlook_client_secret``,
    ``outlook_cert_pem``/``outlook_cert_password``) live in ``system.json`` and are resolved by the
    TOOLBOX (the zero-trust boundary). They are deliberately NOT fields here, exactly as ``JiraConfig``
    omits ``jira_token`` despite the ``obscure`` schema. Bootstrap stores this dataclass in
    ``_MODULE_CONFIGS``, so keeping the secret out of it means the framework registry never holds it.

    The mailbox UPN (``outlook_mailbox_user_id``) is a NON-secret string and is likewise not a field
    here; the Python publisher reads it directly via ``ScopedConfigService`` (per-path ``.get()``) to
    group views by mailbox before polling. The toolbox also resolves it for the actual Graph calls.
    """

    enabled: bool = False
    poll_top: int = 10
    allowed_senders: str = ""
    activation_modes: str = "direct,mention"
    summon_token: str = "@agento"
    direct_requires_sole_recipient: bool = True
    mailbox_aliases: str = ""
    allow_bot_collaboration: bool = False

    @staticmethod
    def _as_bool(value: object, default: bool) -> bool:
        # 3-level config (ENV/DB) returns STRINGS, so bool("0")/bool("false") would be True.
        # Mirror JiraConfig.from_dict's truthiness set; a missing/None value falls back to `default`.
        if value is None:
            return default
        return value not in (False, 0, "0", "false", "False")

    @classmethod
    def from_dict(cls, data: dict) -> OutlookConfig:
        enabled = cls._as_bool(data.get("enabled"), False)
        # poll_top arrives as str/int/garbage — parse defensively and clamp to Graph's 1..50 contract.
        # A missing/None value falls back to 10; an explicit 0 (or negative) clamps to 1.
        poll_top_raw = data.get("poll_top", 10)
        if poll_top_raw is None:
            poll_top_raw = 10
        try:
            poll_top = int(poll_top_raw)
        except (TypeError, ValueError):
            poll_top = 10
        poll_top = min(max(poll_top, 1), 50)
        allowed_senders = data.get("allowed_senders") or ""
        activation_modes = data.get("activation_modes") or "direct,mention"
        summon_token = data.get("summon_token") or "@agento"
        mailbox_aliases = data.get("mailbox_aliases") or ""
        # NOTE: tenant/client/secret/cert/mailbox keys in `data` are intentionally ignored here — they
        # are the toolbox's concern. Do not add them as fields. Loop detection is likewise toolbox-side:
        # the toolbox auto-derives the fleet mailbox set from the agent_views (every OTHER outlook-enabled
        # view's resolved mailbox), matches the inbound From, and only the distilled `agent_authored`
        # boolean reaches Python.
        return cls(
            enabled=enabled,
            poll_top=poll_top,
            allowed_senders=allowed_senders,
            activation_modes=activation_modes,
            summon_token=summon_token,
            direct_requires_sole_recipient=cls._as_bool(data.get("direct_requires_sole_recipient"), True),
            mailbox_aliases=mailbox_aliases,
            allow_bot_collaboration=cls._as_bool(data.get("allow_bot_collaboration"), False),
        )

    @property
    def allowed_senders_list(self) -> list[str]:
        return [s.strip().lower() for s in self.allowed_senders.split(",") if s.strip()]

    @property
    def activation_modes_set(self) -> set[str]:
        return {s.strip().lower() for s in self.activation_modes.split(",") if s.strip()}

    @property
    def mailbox_aliases_list(self) -> list[str]:
        return [s.strip().lower() for s in self.mailbox_aliases.split(",") if s.strip()]

    @property
    def toolbox_url(self) -> str:
        from agento.framework.bootstrap import get_module_config

        core_cfg = get_module_config("core")
        if isinstance(core_cfg, dict):
            return core_cfg.get("toolbox/url", "") or ""
        return ""
