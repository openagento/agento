"""Delivery of a job's attachments into its thread's `shared/` tree (AG-87).

The descriptor is the only thing the framework reads; it never asks the module that wrote
it what it means. The destination is a tree every run of the thread may write, so every
test here is really the same question: can a turn that plants a symlink or a half-copy make
the next delivery write outside the thread, or read a file that is not there any more?
"""
import os
from unittest.mock import patch

import pytest

from agento.framework import artifacts_dir
from agento.framework.artifacts_dir import (
    AttachmentError,
    deliver_attachments,
    parse_attachments,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace whose `uploads/` holds one staged file."""
    monkeypatch.setattr(artifacts_dir, "BASE_WORKSPACE_DIR", str(tmp_path))
    staged = tmp_path / "uploads" / "t1" / "u2" / "abc"
    staged.mkdir(parents=True)
    (staged / "report.pdf").write_bytes(b"pdf bytes")
    return tmp_path


def descriptor(key="m7", source="uploads/t1/u2/abc/report.pdf", name="report.pdf"):
    return f'{{"key": "{key}", "files": [{{"from": "{source}", "as": "{name}"}}]}}'


class TestParse:
    def test_no_descriptor_is_not_an_error(self):
        assert parse_attachments(None) is None
        assert parse_attachments("") is None

    def test_a_valid_descriptor_resolves_to_real_files(self, workspace):
        key, files = parse_attachments(descriptor())
        assert key == "m7"
        # Still relative: the file is resolved only when it is about to be copied.
        assert files == [("uploads/t1/u2/abc/report.pdf", "report.pdf")]

    @pytest.mark.parametrize("raw", [
        "not json",
        '["m7"]',
        '{"key": "m7"}',                                     # no files
        '{"key": "../escape", "files": [{"from": "uploads/t1/u2/abc/report.pdf", "as": "a"}]}',
        '{"key": "m7", "files": [{"from": "uploads/t1/u2/abc/report.pdf", "as": "../a"}]}',
        '{"key": "m7", "files": [{"from": "../../etc/passwd", "as": "a"}]}',
        '{"key": "m7", "files": [{"from": "/etc/passwd", "as": "a"}]}',
    ])
    def test_a_descriptor_that_is_not_safe_fails_the_run(self, workspace, raw):
        with pytest.raises(AttachmentError):
            parse_attachments(raw)


class TestSource:
    """A source is checked when it is about to be read, against the real filesystem."""

    @pytest.mark.parametrize("source", [
        "artifacts/x/y",                          # outside uploads/
        "uploads/t1/u2/abc/gone.pdf",             # not there
        "uploads/t1/u2/abc",                      # a directory
    ])
    def test_a_source_that_is_not_an_uploaded_file_fails_the_run(self, workspace, tmp_path, source):
        with pytest.raises(AttachmentError):
            deliver_attachments(descriptor(source=source), tmp_path / "shared", gid=os.getgid())

    def test_a_source_that_is_a_symlink_out_of_uploads_is_refused(self, workspace, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("x")
        (workspace / "uploads/t1/u2/abc/link.pdf").symlink_to(secret)
        with pytest.raises(AttachmentError):
            deliver_attachments(descriptor(source="uploads/t1/u2/abc/link.pdf"),
                                tmp_path / "shared", gid=os.getgid())


class TestDeliver:
    def delivered(self, shared):
        return shared / "attachments" / "m7" / "report.pdf"

    def test_the_file_lands_in_the_threads_shared_tree(self, workspace, tmp_path):
        shared = tmp_path / "thread" / "shared"
        deliver_attachments(descriptor(), shared, gid=os.getgid())
        assert self.delivered(shared).read_bytes() == b"pdf bytes"

    def test_the_source_is_copied_and_never_moved(self, workspace, tmp_path):
        """A second message may name the same staged file, and the sweep - not delivery -
        is what empties staging."""
        shared = tmp_path / "thread" / "shared"
        deliver_attachments(descriptor(), shared, gid=os.getgid())
        assert (workspace / "uploads/t1/u2/abc/report.pdf").is_file()

    def test_a_delivered_dir_is_reused_without_reading_the_source(self, workspace, tmp_path):
        """What makes a retry and a regeneration work: by then staging is gone."""
        shared = tmp_path / "thread" / "shared"
        deliver_attachments(descriptor(), shared, gid=os.getgid())
        os.remove(workspace / "uploads/t1/u2/abc/report.pdf")
        deliver_attachments(descriptor(), shared, gid=os.getgid())      # no raise
        assert self.delivered(shared).read_bytes() == b"pdf bytes"

    def test_an_incomplete_delivery_is_redone(self, workspace, tmp_path):
        shared = tmp_path / "thread" / "shared"
        deliver_attachments(descriptor(), shared, gid=os.getgid())
        os.remove(self.delivered(shared))
        deliver_attachments(descriptor(), shared, gid=os.getgid())
        assert self.delivered(shared).read_bytes() == b"pdf bytes"

    def test_a_planted_symlink_cannot_divert_the_delivery(self, workspace, tmp_path):
        """An earlier turn of the thread owns `shared/`, so it can plant `attachments` as a
        link to anywhere. The delivery must refuse it, not follow it (SEC-5)."""
        shared = tmp_path / "thread" / "shared"
        shared.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (shared / "attachments").symlink_to(outside)
        with pytest.raises(RuntimeError, match="symlink"):
            deliver_attachments(descriptor(), shared, gid=os.getgid())
        assert list(outside.iterdir()) == []

    def test_a_failed_copy_leaves_no_half_delivered_dir(self, workspace, tmp_path):
        shared = tmp_path / "thread" / "shared"
        with patch("agento.framework.artifacts_dir.shutil.copyfile", side_effect=OSError("disk")), \
                pytest.raises(OSError):
            deliver_attachments(descriptor(), shared, gid=os.getgid())
        assert list((shared / "attachments").iterdir()) == []

    def test_nothing_happens_without_a_descriptor(self, tmp_path):
        shared = tmp_path / "thread" / "shared"
        deliver_attachments(None, shared)
        assert not shared.exists()


def test_delivery_is_given_the_threads_own_dir_not_the_runs_link(workspace, tmp_path):
    """Every destination is opened with `O_NOFOLLOW`, so handing the run's `./shared` symlink
    to the delivery would fail every real run. Run preparation must pass the thread's dir."""
    thread = tmp_path / "_threads" / "k" / "shared"
    thread.mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir()
    (run / "shared").symlink_to(thread)
    with pytest.raises(OSError):
        deliver_attachments(descriptor(), run / "shared", gid=os.getgid())
    deliver_attachments(descriptor(), thread, gid=os.getgid())
    assert (thread / "attachments" / "m7" / "report.pdf").is_file()
