"""Tests for per-job artifacts directory management."""
import logging
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework.artifacts_dir import (
    build_artifacts_dir,
    cleanup_artifacts_dir,
    copy_build_to_artifacts_dir,
    prepare_artifacts_dir,
    prepare_thread_dir,
    thread_dir,
)


class TestBuildArtifactsDir:
    def test_builds_expected_path(self):
        result = build_artifacts_dir("acme", "developer", 42)
        assert result == Path("/workspace/artifacts/acme/developer/42")

    def test_custom_artifacts_dir(self):
        with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", "/tmp/rt"):
            result = build_artifacts_dir("acme", "qa", 99)
        assert result == Path("/tmp/rt/acme/qa/99")


class TestPrepareArtifactsDir:
    def test_creates_directory(self, tmp_path):
        artifacts_dir = tmp_path / "acme" / "dev" / "runs" / "1"
        assert not artifacts_dir.exists()
        prepare_artifacts_dir(artifacts_dir)
        assert artifacts_dir.is_dir()

    def test_idempotent(self, tmp_path):
        artifacts_dir = tmp_path / "runs" / "1"
        prepare_artifacts_dir(artifacts_dir)
        prepare_artifacts_dir(artifacts_dir)
        assert artifacts_dir.is_dir()

    def test_cleans_stale_contents_on_retry(self, tmp_path):
        artifacts_dir = tmp_path / "runs" / "1"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "app").symlink_to(tmp_path)
        (artifacts_dir / "leftover.txt").write_text("stale")
        prepare_artifacts_dir(artifacts_dir)
        assert artifacts_dir.is_dir()
        assert not (artifacts_dir / "app").exists()
        assert not (artifacts_dir / "leftover.txt").exists()

    def test_permission_error_logs_ownership_and_reraises(self, tmp_path, caplog):
        # Reproduces the toolbox-as-root vs cron-as-agent ownership mismatch:
        # rmtree fails with PermissionError, we want the log to surface
        # ownership info so the next ownership regression is diagnosable
        # without manual `ls -la` archaeology.
        artifacts_dir = tmp_path / "runs" / "1"
        artifacts_dir.mkdir(parents=True)
        offender = artifacts_dir / "jira" / "AI-76"
        offender.mkdir(parents=True)

        boom = PermissionError(13, "Permission denied", str(offender))
        with (
            patch("shutil.rmtree", side_effect=boom),
            caplog.at_level(logging.ERROR, logger="agento.framework.artifacts_dir"),
            pytest.raises(PermissionError),
        ):
            prepare_artifacts_dir(artifacts_dir)

        assert "owner uid=" in caplog.text
        assert str(offender) in caplog.text
        assert "Process running as uid=" in caplog.text


class TestCleanupArtifactsDir:
    def test_removes_directory(self, tmp_path):
        artifacts_dir = tmp_path / "runs" / "1"
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / ".claude.json").write_text("{}")
        cleanup_artifacts_dir(artifacts_dir)
        assert not artifacts_dir.exists()

    def test_noop_if_missing(self, tmp_path):
        artifacts_dir = tmp_path / "nonexistent"
        cleanup_artifacts_dir(artifacts_dir)  # should not raise

    def test_logs_warning_on_error(self, tmp_path, caplog):
        artifacts_dir = tmp_path / "runs" / "1"
        artifacts_dir.mkdir(parents=True)
        with patch("shutil.rmtree", side_effect=OSError("permission denied")):
            cleanup_artifacts_dir(artifacts_dir)
        assert "Failed to clean up" in caplog.text


class TestCopyBuildToArtifactsDir:
    def test_gitconfig_is_copied_not_symlinked(self, tmp_path):
        # .gitconfig must be a private per-run COPY, not a symlink into the shared build — otherwise a
        # run-time `git config` write would follow the link and corrupt the build for future runs.
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / ".gitconfig").write_text('[user]\n\tname = "X"\n')
        ssh = build_dir / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text("KEY")
        artifacts_dir = tmp_path / "art"
        artifacts_dir.mkdir()

        copy_build_to_artifacts_dir(build_dir, artifacts_dir)  # no provider/job_id

        gc = artifacts_dir / ".gitconfig"
        assert gc.is_file() and not gc.is_symlink()
        assert gc.read_text() == '[user]\n\tname = "X"\n'
        # .ssh stays symlinked (keys are read-only-used; only .gitconfig is at write-through risk).
        assert (artifacts_dir / ".ssh").is_symlink()


class TestRunDirCodes:
    """A workspace or agent_view code names a dir next to `_threads`: one starting with `_`
    (or not a code at all) names no run dir, whatever path wrote the row."""

    @pytest.mark.parametrize(("ws", "av"), [("_threads", "dev"), ("acme", "_fallback"),
                                            ("acme\n", "dev"), ("..", "dev"), ("acme", "a/b")])
    def test_a_code_that_is_not_valid_names_no_run_dir(self, ws, av):
        with pytest.raises(ValueError, match="cannot name a run dir"):
            build_artifacts_dir(ws, av, 1)

    def test_a_code_made_by_hand_with_a_dash_still_runs(self):
        assert build_artifacts_dir("e2-tc", "Dev-1", 1).parts[-3:] == ("e2-tc", "Dev-1", "1")


class TestPrepareThreadDir:
    """The thread dirs (docs/architecture/threads.md): modes, the link, and SEC-5 refusals."""

    KEY = "0123456789abcdef0123456789abcdef"

    @pytest.fixture
    def root(self, tmp_path):
        root = tmp_path / "artifacts"
        run = root / "acme" / "dev" / "7"
        run.mkdir(parents=True)
        with patch("agento.framework.artifacts_dir.ARTIFACTS_DIR", str(root)):
            yield root, run

    def _prepare(self, run):
        return prepare_thread_dir(self.KEY, run, gid=os.getgid())

    def test_creates_the_dirs_with_their_modes_and_the_link(self, root):
        base, run = root
        thread = self._prepare(run)
        assert thread == base / "_threads" / self.KEY
        modes = {p: stat.S_IMODE(os.lstat(p).st_mode) for p in (
            base / "_threads", thread, thread / "shared", thread / "downloads")}
        assert modes == {base / "_threads": 0o711, thread: 0o2750,
                         thread / "shared": 0o2770, thread / "downloads": 0o2750}
        link = run / "shared"
        assert link.is_symlink() and os.readlink(link) == str(thread / "shared")
        assert os.path.isabs(os.readlink(link))

    def test_a_retry_keeps_the_threads_files(self, root):
        _base, run = root
        thread = self._prepare(run)
        (run / "shared" / "notes.md").write_text("turn 1")
        prepare_artifacts_dir(run)  # the retry wipes the run dir: the link, not its target
        assert not (run / "shared").exists()
        self._prepare(run)
        assert (thread / "shared" / "notes.md").read_text() == "turn 1"
        assert (run / "shared" / "notes.md").read_text() == "turn 1"

    def test_the_link_replaces_a_build_entry_named_shared(self, root):
        _base, run = root
        (run / "shared").mkdir()
        (run / "shared" / "x").write_text("from the build")
        self._prepare(run)
        assert (run / "shared").is_symlink() and not (run / "shared" / "x").exists()

    @pytest.mark.parametrize("planted", ["_threads", "key", "shared", "downloads"])
    def test_a_planted_symlink_component_is_refused(self, root, tmp_path, planted):
        base, run = root
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        threads = base / "_threads"
        if planted == "_threads":
            threads.symlink_to(elsewhere)
        else:
            threads.mkdir(mode=0o711)
            key = threads / self.KEY
            if planted == "key":
                key.symlink_to(elsewhere)
            else:
                key.mkdir()
                (key / planted).symlink_to(elsewhere)
        with pytest.raises(RuntimeError, match="symlink"):
            self._prepare(run)
        assert list(elsewhere.iterdir()) == []
        assert not (run / "shared").exists()

    @pytest.mark.parametrize("key", ["../../etc", "ABCDEF0123456789ABCDEF0123456789", "", "a" * 31])
    def test_a_key_that_is_not_32_hex_is_refused(self, key):
        with pytest.raises(ValueError):
            thread_dir(key)

    def test_a_host_not_in_the_thread_group_fails_the_run(self, root, monkeypatch):
        _base, run = root

        def refuse(*_a):
            raise PermissionError(1, "Operation not permitted")
        monkeypatch.setattr(os, "fchown", refuse)
        with pytest.raises(RuntimeError, match="thread group"):
            prepare_thread_dir(self.KEY, run, gid=12345)

    def test_the_thread_group_is_the_default(self, root, monkeypatch):
        from agento.framework.runner import uids
        _base, run = root
        seen = []
        monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: seen.append(gid))
        prepare_thread_dir(self.KEY, run)
        assert seen == [-1, uids.THREAD_GID, uids.THREAD_GID, uids.THREAD_GID]
