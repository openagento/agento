"""`workspace:ssh-purge` — the one-time admin sweep for keys already on disk (AC3)."""
from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from agento.modules.workspace_build.src.commands.ssh_purge import SshPurgeCommand

_PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQy\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace tree shaped like the one the incident ran on: builds, artifacts, state."""
    build_key = tmp_path / "build" / "acme" / "peer_view" / "builds" / "15" / ".ssh" / "id_rsa"
    build_key.parent.mkdir(parents=True)
    build_key.write_text(_PEM)
    (build_key.parent / "id_rsa.pub").write_text("pub")

    # A stolen copy in another view's run dir, renamed — found by content, not by name.
    stolen = tmp_path / "artifacts" / "acme" / "thief_view" / "42" / "work" / ".sshkey" / "id_rsa"
    stolen.parent.mkdir(parents=True)
    stolen.write_text(_PEM)

    truncated = tmp_path / "build" / "acme" / "peer_view" / "builds" / "4" / ".ssh" / "id_rsa"
    truncated.parent.mkdir(parents=True)
    truncated.write_text("-----BEGIN OPENSSH PRIVATE")  # the 36-byte generation

    state = tmp_path / "state" / "id_rsa_old"
    state.parent.mkdir(parents=True)
    state.write_text(_PEM)

    monkeypatch.setattr(
        "agento.framework.workspace_paths.BASE_WORKSPACE_DIR", str(tmp_path),
    )
    return tmp_path, [build_key, stolen, truncated, state]


def _run(dry_run=False, choice=1):
    with patch(
        "agento.framework.cli.terminal.select", return_value=choice,
    ) as mock_select:
        SshPurgeCommand().execute(argparse.Namespace(dry_run=dry_run))
    return mock_select


class TestSshPurge:
    def test_deletes_keys_in_builds_artifacts_and_state(self, workspace, capsys):
        _root, planted = workspace
        _run()
        out = capsys.readouterr().out
        for path in planted:
            assert not path.exists(), path
            assert str(path) in out
        # Non-secret files survive.
        assert (planted[0].parent / "id_rsa.pub").is_file()
        assert "ROTATE" in out

    def test_dry_run_deletes_nothing_and_still_lists(self, workspace, capsys):
        _root, planted = workspace
        mock_select = _run(dry_run=True)
        out = capsys.readouterr().out
        assert all(p.exists() for p in planted)
        assert "nothing deleted" in out
        mock_select.assert_not_called()

    def test_declining_the_confirmation_deletes_nothing(self, workspace, capsys):
        _root, planted = workspace
        _run(choice=0)
        assert all(p.exists() for p in planted)
        assert "Cancelled" in capsys.readouterr().out

    def test_a_rerun_on_a_clean_tree_reports_zero(self, workspace, capsys):
        _run()
        capsys.readouterr()
        _run()
        assert "No private keys found" in capsys.readouterr().out

    def test_a_source_file_quoting_a_pem_header_is_not_reported(self, workspace, capsys):
        """An agent clones repositories into workspace/artifacts — including this one."""
        root, _ = workspace
        source = root / "artifacts" / "acme" / "thief_view" / "42" / "repo" / "test_x.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            'import pytest\n\n_KEY = """-----BEGIN OPENSSH PRIVATE KEY-----\n'
            'AAAA\n-----END OPENSSH PRIVATE KEY-----\n"""\n'
        )
        _run()
        assert str(source) not in capsys.readouterr().out
        assert source.is_file()

    def test_a_large_file_whose_prefix_is_a_key_is_still_found(self, workspace, capsys):
        """No size skip: an earlier revision skipped >64 KiB, so a padded key survived."""
        root, _ = workspace
        padded = root / "artifacts" / "padded.pem"
        padded.write_text(_PEM + "A" * 200_000)
        _run()
        assert str(padded) in capsys.readouterr().out
        assert not padded.exists()

    def test_symlinks_are_not_followed(self, workspace, capsys):
        root, _ = workspace
        outside = root.parent / "outside_key"
        outside.write_text(_PEM)
        link = root / "artifacts" / "linked_id_rsa"
        link.symlink_to(outside)
        _run()
        assert outside.exists()
        assert str(link) not in capsys.readouterr().out

    def test_states_its_format_scope_and_the_deploy_order(self, workspace, capsys):
        """The order is deploy-then-purge: the OLD code re-wrote the key on the next build.

        It must not tell an operator to stop the consumer — the host CLI reaches this
        command by exec'ing into the running cron container.
        """
        _run(dry_run=True)
        out = capsys.readouterr().out
        assert "not an exfiltration detector" in out
        assert "deploy this release first" in out.lower()
        assert "consumer may keep running" in out

    def test_announces_its_own_deprecation_with_the_target_release(self, workspace, capsys):
        """A deprecated command must say so where the operator is, not only in the docs.

        The removal target lives in ROADMAP.md; the printed notice and `help` must name it
        so an operator running the command learns it is temporary.
        """
        _run(dry_run=True)
        out = capsys.readouterr().out
        assert "DEPRECATED" in out
        assert "v0.17" in out
        assert "v0.17" in SshPurgeCommand().help

    def test_the_deprecation_notice_prints_without_a_workspace_dir(
        self, tmp_path, monkeypatch, capsys
    ):
        """The notice precedes the no-workspace early return — that operator needs it too."""
        monkeypatch.setattr(
            "agento.framework.workspace_paths.BASE_WORKSPACE_DIR",
            str(tmp_path / "absent"),
        )
        _run()
        assert "DEPRECATED" in capsys.readouterr().out

    def test_no_workspace_dir_is_not_an_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "agento.framework.workspace_paths.BASE_WORKSPACE_DIR",
            str(tmp_path / "absent"),
        )
        SshPurgeCommand().execute(argparse.Namespace(dry_run=False))
        assert "nothing to purge" in capsys.readouterr().out

    def test_an_unscannable_path_is_reported_and_exits_non_zero(self, workspace, capsys):
        """"No private keys found" is not something an incomplete scan may report."""
        root, planted = workspace
        for path in planted:
            path.unlink()
        sealed = root / "artifacts" / "sealed"
        sealed.mkdir()
        (sealed / "id_rsa").write_text(_PEM)
        sealed.chmod(0o000)
        try:
            with pytest.raises(SystemExit) as exc:
                _run()
        finally:
            sealed.chmod(0o700)
        out = capsys.readouterr().out
        assert "Could NOT scan" in out and "sealed" in out
        assert "Scan incomplete" in str(exc.value)
        # An incomplete scan may not claim absence, not even alongside the warning.
        assert "No private keys found" not in out

    def test_an_incomplete_dry_run_exits_non_zero(self, workspace, capsys):
        """A dry run is a report; a report built from an unreadable tree is incomplete."""
        root, planted = workspace
        sealed = root / "artifacts" / "sealed"
        sealed.mkdir()
        (sealed / "id_rsa").write_text(_PEM)
        sealed.chmod(0o000)
        try:
            with pytest.raises(SystemExit) as exc:
                _run(dry_run=True)
        finally:
            sealed.chmod(0o700)
        out = capsys.readouterr().out
        assert "Could NOT scan" in out and str(planted[0]) in out
        assert "incomplete" in str(exc.value)
        assert all(p.exists() for p in planted)

    def test_cancelling_after_an_incomplete_scan_exits_non_zero(self, workspace, capsys):
        """The human declined from a list that was not the whole list."""
        root, planted = workspace
        sealed = root / "artifacts" / "sealed2"
        sealed.mkdir()
        (sealed / "id_rsa").write_text(_PEM)
        sealed.chmod(0o000)
        try:
            with pytest.raises(SystemExit) as exc:
                _run(choice=0)
        finally:
            sealed.chmod(0o700)
        assert "Cancelled" in capsys.readouterr().out
        assert "unscannable" in str(exc.value)
        assert all(p.exists() for p in planted)

    def test_a_key_that_cannot_be_deleted_exits_non_zero(self, workspace, capsys):
        _root, planted = workspace
        stuck = planted[0].parent
        stuck.chmod(0o500)
        try:
            with pytest.raises(SystemExit) as exc:
                _run()
        finally:
            stuck.chmod(0o700)
        assert "unresolved" in str(exc.value)
        assert planted[0].exists()  # and it is still reported as present
        assert "ROTATE" in capsys.readouterr().out

    def test_offers_no_yes_flag(self):
        parser = argparse.ArgumentParser()
        SshPurgeCommand().configure(parser)
        flags = {a for action in parser._actions for a in action.option_strings}
        assert "--dry-run" in flags
        assert "--yes" not in flags and "-y" not in flags
