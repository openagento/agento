"""Staging of composer uploads (AG-87).

Staging is the record of what a user uploaded and has not yet sent. Everything here is
about who may use a staged file: the id alone is not a right, the thread and the user are
part of the address, and a name the browser sent is never a path.
"""
import os
from pathlib import Path

import pytest

from agento.modules.conversation.src import uploads


@pytest.fixture
def staging(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOADS_DIR", str(tmp_path / "uploads"))
    return tmp_path / "uploads"


class TestSafeName:
    @pytest.mark.parametrize("raw,expected", [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        ("a/b/c.txt", "c.txt"),
        (".hidden", "hidden"),
        ("", "file"),
        (None, "file"),
    ])
    def test_a_browser_name_never_becomes_a_path(self, raw, expected):
        assert uploads.safe_name(raw) == expected


def test_two_files_of_one_message_keep_distinct_names():
    taken = {"a.pdf"}
    assert uploads.unique_name("a.pdf", taken) == "a-2.pdf"
    assert uploads.unique_name("b.pdf", taken) == "b.pdf"


class TestStore:
    def test_a_symlinked_uploads_root_is_refused(self, staging, tmp_path):
        """The root gets the same check as the components below it (SEC-5)."""
        (tmp_path / "elsewhere").mkdir()
        staging.symlink_to(tmp_path / "elsewhere")
        with pytest.raises(uploads.UploadError):
            uploads.store(7, 3, "report.pdf", b"bytes")
        assert not (tmp_path / "elsewhere" / "t7").exists()

    def test_a_stored_file_is_private_to_its_user(self, staging):
        upload_id, stored = uploads.store(7, 3, "report.pdf", b"bytes")
        path = uploads.user_dir(7, 3) / upload_id / stored
        assert path.read_bytes() == b"bytes"
        assert oct(path.stat().st_mode)[-3:] == "600"
        assert oct(uploads.user_dir(7, 3).stat().st_mode)[-3:] == "700"

    def test_the_thread_and_the_user_are_part_of_the_address(self, staging):
        upload_id, _ = uploads.store(7, 3, "report.pdf", b"bytes")
        # Another user of the same thread, and the same user in another thread: neither can
        # name this id, because the directory they are looked up in is a different one.
        for conversation_id, user_id in ((7, 4), (8, 3)):
            with pytest.raises(uploads.UploadError):
                uploads.descriptor(conversation_id, user_id, [upload_id], "m1")

    def test_an_unknown_id_refuses_the_submission(self, staging):
        with pytest.raises(uploads.UploadError):
            uploads.descriptor(7, 3, ["0" * 32], "m1")

    def test_an_id_that_is_not_an_id_refuses_the_submission(self, staging):
        with pytest.raises(uploads.UploadError):
            uploads.descriptor(7, 3, ["../../../etc"], "m1")


class TestDescriptor:
    def test_the_descriptor_is_relative_to_the_workspace(self, staging):
        first, _ = uploads.store(7, 3, "report.pdf", b"a")
        second, _ = uploads.store(7, 3, "report.pdf", b"b")
        doc = uploads.descriptor(7, 3, [first, second], "m12")
        assert doc["key"] == "m12"
        assert [f["as"] for f in doc["files"]] == ["report.pdf", "report-2.pdf"]
        for file in doc["files"]:
            assert file["from"].startswith("uploads/t7/u3/")
            assert not Path(file["from"]).is_absolute()

    def test_the_prompt_names_the_runs_own_paths(self):
        doc = {"key": "m12", "files": [{"from": "uploads/x", "as": "report.pdf"}]}
        assert uploads.prompt_block(doc) == (
            "\n\nAttached files:\n- ./shared/attachments/m12/report.pdf")


class TestParseOneFile:
    def body(self, filename="report.pdf", payload=b"bytes"):
        boundary = "X"
        raw = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
               f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
               ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        return raw, f"multipart/form-data; boundary={boundary}"

    def test_one_file_part_is_read(self):
        raw, ctype = self.body()
        assert uploads.parse_one_file(raw, ctype) == ("report.pdf", b"bytes")

    def test_a_json_body_is_refused(self):
        with pytest.raises(uploads.UploadError):
            uploads.parse_one_file(b"{}", "application/json")

    def test_an_empty_file_is_refused(self):
        raw, ctype = self.body(payload=b"")
        with pytest.raises(uploads.UploadError):
            uploads.parse_one_file(raw, ctype)

    def test_a_body_with_no_file_part_is_refused(self):
        with pytest.raises(uploads.UploadError):
            uploads.parse_one_file(b"--X--\r\n", "multipart/form-data; boundary=X")


class TestSweepHelpers:
    def test_only_the_dirs_past_the_retention_are_listed(self, staging):
        fresh, _ = uploads.store(7, 3, "a.pdf", b"a")
        old, _ = uploads.store(7, 3, "b.pdf", b"b")
        path = uploads.user_dir(7, 3) / old
        long_ago = os.path.getmtime(path) - 48 * 3600
        os.utime(path, (long_ago, long_ago))
        assert uploads.expired_dirs(7, 3, 24) == [old]
        assert fresh not in uploads.expired_dirs(7, 3, 24)

    def test_the_sweep_finds_every_thread_and_user_that_has_staging(self, staging):
        uploads.store(7, 3, "a.pdf", b"a")
        uploads.store(9, 4, "b.pdf", b"b")
        assert sorted(uploads.conversations_with_uploads()) == [7, 9]
        assert uploads.users_with_uploads(7) == [3]

    def test_remove_takes_one_dir_and_refuses_a_path(self, staging):
        upload_id, _ = uploads.store(7, 3, "a.pdf", b"a")
        uploads.remove(7, 3, "../u3")
        assert uploads.user_dir(7, 3).is_dir()
        uploads.remove(7, 3, upload_id)
        assert not (uploads.user_dir(7, 3) / upload_id).exists()
