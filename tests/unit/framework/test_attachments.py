"""Delivery of a job's attachments into its thread's `shared/` tree (AG-87).

The descriptor is the only thing the framework reads; it never asks the module that wrote
it what it means. The destination is a tree every run of the thread may write, so every
test here is really the same question: can a turn that plants a symlink or a half-copy make
the next delivery write outside the thread, or read a file that is not there any more?
"""
import contextlib
import os
import shutil
from unittest.mock import patch

import pytest

from agento.framework import artifacts_dir, run_preparation
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

    def test_a_symlinked_uploads_root_is_refused(self, workspace, tmp_path):
        """The root itself is a check, not a prefix: a link there steers every source."""
        elsewhere = tmp_path / "elsewhere" / "t1" / "u2" / "abc"
        elsewhere.mkdir(parents=True)
        (elsewhere / "report.pdf").write_bytes(b"other bytes")
        shutil.rmtree(workspace / "uploads")
        (workspace / "uploads").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(AttachmentError):
            deliver_attachments(descriptor(), tmp_path / "shared", gid=os.getgid())


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
        with patch("agento.framework.artifacts_dir.shutil.copyfileobj", side_effect=OSError("disk")), \
                pytest.raises(OSError):
            deliver_attachments(descriptor(), shared, gid=os.getgid())
        assert list((shared / "attachments").iterdir()) == []

    def test_a_key_planted_as_a_symlink_is_never_a_delivery(self, workspace, tmp_path):
        """`<key>` is as plantable as `attachments/`: both live under the thread's `shared/`,
        which every run of the thread may write (SEC-5)."""
        shared = tmp_path / "thread" / "shared"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "report.pdf").write_bytes(b"theirs")
        (shared / "attachments").mkdir(parents=True)
        (shared / "attachments" / "m7").symlink_to(outside)
        with pytest.raises(OSError):
            deliver_attachments(descriptor(), shared, gid=os.getgid())
        assert (outside / "report.pdf").read_bytes() == b"theirs"   # never written through

    def test_the_dir_cannot_be_swapped_between_the_check_and_the_copy(self, workspace, tmp_path):
        """The window an fd closes: `attachments/` passes its check, and an earlier run of the
        thread then replaces it with a link to its own dir before a byte is written. A
        path-string copy would write, chown and rename THERE; an fd stays on the inode that
        was checked, so the swap reaches nothing (SEC-5)."""
        shared = tmp_path / "thread" / "shared"
        outside = tmp_path / "outside"
        outside.mkdir()
        real = artifacts_dir._source_path

        def swap(source):
            link = shared / "attachments"
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link)
                link.symlink_to(outside)
            return real(source)

        with patch.object(artifacts_dir, "_source_path", side_effect=swap), \
                contextlib.suppress(OSError):
            deliver_attachments(descriptor(), shared, gid=os.getgid())
        assert list(outside.iterdir()) == []       # the swap reached nothing

    @pytest.mark.parametrize("name", ["my report.pdf", "zazolc gesla.txt", "(report).pdf",
                                      "rapport-é.pdf"])
    def test_a_name_a_user_may_really_give_a_file_is_delivered(self, workspace, tmp_path, name):
        """`safe_name` keeps spaces, punctuation and non-ASCII letters; a stricter rule here
        would stage the file and then fail the run (BUG)."""
        (workspace / "uploads/t1/u2/abc" / name).write_bytes(b"pdf bytes")
        shared = tmp_path / "thread" / "shared"
        deliver_attachments(descriptor(source=f"uploads/t1/u2/abc/{name}", name=name),
                            shared, gid=os.getgid())
        assert (shared / "attachments" / "m7" / name).read_bytes() == b"pdf bytes"

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



class TestRunPreparationBranch:
    """Where run preparation sends the delivery, with and without a thread."""

    @pytest.fixture(autouse=True)
    def own_group(self):
        """The real THREAD_GID is the container's; here the test process is the group."""
        with patch("agento.framework.runner.uids.THREAD_GID", os.getgid()):
            yield

    def test_a_thread_gets_the_threads_own_dir(self, workspace, tmp_path):
        thread = tmp_path / "_threads" / "k" / "shared"
        thread.mkdir(parents=True)
        run = tmp_path / "run"
        run.mkdir()
        with patch.object(run_preparation, "prepare_thread_dir",
                          return_value=thread.parent) as prepared:
            run_preparation._link_thread_and_deliver("k", descriptor(), run)
        prepared.assert_called_once()
        assert (thread / "attachments" / "m7" / "report.pdf").read_bytes() == b"pdf bytes"

    def test_a_job_with_no_thread_is_delivered_under_its_own_run_dir(self, workspace, tmp_path):
        """The fail-safe branch: the panel always has a thread, but a job that has none must
        still find the files its prompt names."""
        run = tmp_path / "run"
        (run / "shared").mkdir(parents=True)
        with patch.object(run_preparation, "prepare_thread_dir") as prepared:
            run_preparation._link_thread_and_deliver(None, descriptor(), run)
        prepared.assert_not_called()
        assert (run / "shared" / "attachments" / "m7" / "report.pdf").read_bytes() == b"pdf bytes"

    def test_no_descriptor_still_links_the_thread(self, workspace, tmp_path):
        run = tmp_path / "run"
        run.mkdir()
        with patch.object(run_preparation, "prepare_thread_dir",
                          return_value=tmp_path) as prepared:
            run_preparation._link_thread_and_deliver("k", None, run)
        prepared.assert_called_once()
