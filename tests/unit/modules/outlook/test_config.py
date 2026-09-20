from unittest.mock import patch

from agento.modules.outlook.src.config import OutlookConfig


def test_from_dict_maps_python_fields_and_drops_secrets():
    c = OutlookConfig.from_dict({
        "enabled": True,
        "outlook_tenant_id": "tid",
        "outlook_client_id": "cid",
        "outlook_client_secret": "sec",
        "outlook_cert_pem": "-----BEGIN CERTIFICATE-----\n...",
        "outlook_mailbox_user_id": "agent@example.com",
        "poll_top": 25,
    })
    assert c.enabled is True
    assert c.poll_top == 25
    # Graph secrets/cert/mailbox are toolbox-only — they must NOT become attributes of the Python config.
    assert not hasattr(c, "outlook_client_secret")
    assert not hasattr(c, "outlook_cert_pem")
    assert not hasattr(c, "outlook_tenant_id")
    assert not hasattr(c, "outlook_mailbox_user_id")


def test_defaults():
    c = OutlookConfig.from_dict({})
    assert c.enabled is False
    assert c.poll_top == 10
    assert c.allowed_senders == ""
    assert c.allowed_senders_list == []


def test_enabled_parses_stringy_falsey_values():
    # DB/ENV give strings — "0"/"false"/"False"/0/False must all disable.
    for v in ("0", "false", "False", 0, False):
        assert OutlookConfig.from_dict({"enabled": v}).enabled is False
    for v in ("1", "true", True):
        assert OutlookConfig.from_dict({"enabled": v}).enabled is True


def test_poll_top_is_defensive_and_clamped():
    assert OutlookConfig.from_dict({"poll_top": "999"}).poll_top == 50   # clamp high
    assert OutlookConfig.from_dict({"poll_top": 0}).poll_top == 1        # clamp low
    assert OutlookConfig.from_dict({"poll_top": "abc"}).poll_top == 10   # garbage -> default


def test_allowed_senders_list_normalizes_and_splits():
    c = OutlookConfig.from_dict({"allowed_senders": " Foo@Bar.com , *@Mycompany.com ,, "})
    assert c.allowed_senders == " Foo@Bar.com , *@Mycompany.com ,, "
    assert c.allowed_senders_list == ["foo@bar.com", "*@mycompany.com"]


def test_allowed_senders_list_empty_for_blank():
    assert OutlookConfig.from_dict({"allowed_senders": "   "}).allowed_senders_list == []
    assert OutlookConfig.from_dict({}).allowed_senders_list == []


def test_thread_read_max_messages_is_defensive_and_clamped():
    assert OutlookConfig.from_dict({}).thread_read_max_messages == 50        # default
    assert OutlookConfig.from_dict({"thread_read_max_messages": "999"}).thread_read_max_messages == 200  # clamp high
    assert OutlookConfig.from_dict({"thread_read_max_messages": 0}).thread_read_max_messages == 1        # clamp low
    assert OutlookConfig.from_dict({"thread_read_max_messages": "abc"}).thread_read_max_messages == 50   # garbage -> default


def test_toolbox_url_reads_core_config_when_dict():
    with patch("agento.framework.bootstrap.get_module_config", return_value={"toolbox/url": "http://tb:3001"}):
        assert OutlookConfig.from_dict({}).toolbox_url == "http://tb:3001"


def test_toolbox_url_empty_when_core_missing_or_not_dict():
    # core unset -> "" (no crash)
    with patch("agento.framework.bootstrap.get_module_config", return_value=None):
        assert OutlookConfig.from_dict({}).toolbox_url == ""

    # core resolved to a dataclass (not a plain dict) -> "" rather than AttributeError
    class _CoreCfg:
        pass

    with patch("agento.framework.bootstrap.get_module_config", return_value=_CoreCfg()):
        assert OutlookConfig.from_dict({}).toolbox_url == ""
