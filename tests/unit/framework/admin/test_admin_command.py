from unittest.mock import patch

from agento.framework.admin import AdminCommand


def test_execute_writes_deprecation_line_to_stderr(capsys):
    with patch("agento.framework.admin.app.AdminApp.run") as run:
        AdminCommand().execute(None)
    run.assert_called_once()
    err = capsys.readouterr().err
    assert "agento admin is deprecated: use the admin panel" in err
    assert "v1.0.0" in err


def test_help_says_deprecated():
    assert "(deprecated)" in AdminCommand().help
