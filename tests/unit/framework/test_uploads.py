"""Staging of uploaded files (AG-87): the framework's part, for every owner.

Staging is the record of what a user uploaded and has not yet sent. Everything here is
about who may use a staged file: the id alone is not a right, the thread and the user are
part of the address, and a name the browser sent is never a path.
"""
import os
from pathlib import Path

import pytest

from agento.framework import uploads


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
            uploads.store("t7", 3, "report.pdf", b"bytes")
        assert not (tmp_path / "elsewhere" / "t7").exists()

    def test_a_stored_file_is_private_to_its_user(self, staging):
        upload_id, stored = uploads.store("t7", 3, "report.pdf", b"bytes")
        path = uploads.user_dir("t7", 3) / upload_id / stored
        assert path.read_bytes() == b"bytes"
        assert oct(path.stat().st_mode)[-3:] == "600"
        assert oct(uploads.user_dir("t7", 3).stat().st_mode)[-3:] == "700"

    def test_the_thread_and_the_user_are_part_of_the_address(self, staging):
        upload_id, _ = uploads.store("t7", 3, "report.pdf", b"bytes")
        # Another user of the same thread, and the same user in another thread: neither can
        # name this id, because the directory they are looked up in is a different one.
        for owner, user_id in (("t7", 4), ("t8", 3), ("s7", 3)):
            with pytest.raises(uploads.UploadError):
                uploads.descriptor(owner, user_id, [upload_id], "m1")

    def test_an_unknown_id_refuses_the_submission(self, staging):
        with pytest.raises(uploads.UploadError):
            uploads.descriptor("t7", 3, ["0" * 32], "m1")

    def test_an_id_that_is_not_an_id_refuses_the_submission(self, staging):
        with pytest.raises(uploads.UploadError):
            uploads.descriptor("t7", 3, ["../../../etc"], "m1")


class TestDescriptor:
    def test_the_descriptor_is_relative_to_the_workspace(self, staging):
        first, _ = uploads.store("t7", 3, "report.pdf", b"a")
        second, _ = uploads.store("t7", 3, "report.pdf", b"b")
        doc = uploads.descriptor("t7", 3, [first, second], "m12")
        assert doc["key"] == "m12"
        assert [f["as"] for f in doc["files"]] == ["report.pdf", "report-2.pdf"]
        for file in doc["files"]:
            assert file["from"].startswith("uploads/t7/u3/")
            assert not Path(file["from"]).is_absolute()


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
        fresh, _ = uploads.store("t7", 3, "a.pdf", b"a")
        old, _ = uploads.store("t7", 3, "b.pdf", b"b")
        path = uploads.user_dir("t7", 3) / old
        long_ago = os.path.getmtime(path) - 48 * 3600
        os.utime(path, (long_ago, long_ago))
        assert uploads.expired_dirs("t7", 3, 24) == [old]
        assert fresh not in uploads.expired_dirs("t7", 3, 24)

    def test_the_sweep_finds_every_thread_and_user_that_has_staging(self, staging):
        uploads.store("t7", 3, "a.pdf", b"a")
        uploads.store("t9", 4, "b.pdf", b"b")
        assert sorted(uploads.owners("t")) == [7, 9]
        assert uploads.users("t7") == [3]

    def test_remove_takes_one_dir_and_refuses_a_path(self, staging):
        upload_id, _ = uploads.store("t7", 3, "a.pdf", b"a")
        uploads.remove("t7", 3, "../u3")
        assert uploads.user_dir("t7", 3).is_dir()
        uploads.remove("t7", 3, upload_id)
        assert not (uploads.user_dir("t7", 3) / upload_id).exists()


class TestOwner:
    """The owner is ONE validated component: a conversation (`t…`) or a schedule (`s…`)."""

    @pytest.mark.parametrize("owner", ["t7", "s7", "s0"])
    def test_a_file_is_staged_under_its_owner(self, staging, owner):
        upload_id, stored = uploads.store(owner, 3, "report.pdf", b"bytes")
        assert (staging / owner / "u3" / upload_id / stored).read_bytes() == b"bytes"
        assert uploads.descriptor(owner, 3, [upload_id], "k")["files"] == [
            {"from": f"uploads/{owner}/u3/{upload_id}/report.pdf", "as": "report.pdf"}]

    @pytest.mark.parametrize("owner", ["x1", "s", "t", "../s1", "s1/x", "s1 ", "S1", "", 7, None])
    def test_an_owner_that_is_not_one_is_refused(self, staging, owner):
        with pytest.raises(uploads.UploadError):
            uploads.store(owner, 3, "report.pdf", b"bytes")
        assert not staging.exists()

    @pytest.mark.parametrize("owner", ["t7", "s7"])
    @pytest.mark.parametrize("level", ["owner", "user"])
    def test_a_planted_symlink_is_refused(self, staging, tmp_path, owner, level):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        staging.mkdir()
        if level == "owner":
            os.symlink(elsewhere, staging / owner)
        else:
            (staging / owner).mkdir()
            os.symlink(elsewhere, staging / owner / "u3")
        with pytest.raises(uploads.UploadError):
            uploads.store(owner, 3, "report.pdf", b"bytes")
        assert list(elsewhere.iterdir()) == []

    @pytest.mark.parametrize("level", ["owner", "user", "upload", "file"])
    def test_a_link_on_the_way_to_a_staged_file_is_not_followed(self, staging, tmp_path, level):
        """Claim and copy read through fds opened with O_NOFOLLOW at every level (SEC-5)."""
        upload_id = "ab" * 16
        outside = tmp_path / "outside"
        (outside / "u3" / upload_id).mkdir(parents=True)
        (outside / "u3" / upload_id / "secret.pdf").write_bytes(b"outside staging")
        staging.mkdir()
        if level == "owner":
            os.symlink(outside, staging / "s0")
        elif level == "user":
            (staging / "s0").mkdir()
            os.symlink(outside / "u3", staging / "s0" / "u3")
        elif level == "upload":
            (staging / "s0" / "u3").mkdir(parents=True)
            os.symlink(outside / "u3" / upload_id, staging / "s0" / "u3" / upload_id)
        else:
            (staging / "s0" / "u3" / upload_id).mkdir(parents=True)
            os.symlink(outside / "u3" / upload_id / "secret.pdf", staging / "s0" / "u3" / upload_id / "secret.pdf")
            with pytest.raises(uploads.UploadError):
                uploads.copy("s0", "s9", 3, upload_id)
            return                                   # a claim reads no bytes; the copy is the reader
        with pytest.raises(uploads.UploadError):
            uploads.descriptor("s0", 3, [upload_id], "k")
        with pytest.raises(uploads.UploadError):
            uploads.copy("s0", "s9", 3, upload_id)
        assert not (staging / "s9").exists()

    def test_each_prefix_lists_only_its_own_trees(self, staging):
        uploads.store("t7", 3, "a.pdf", b"a")
        uploads.store("s9", 3, "b.pdf", b"b")
        (staging / "s").mkdir()
        (staging / "sx").mkdir()
        assert uploads.owners("t") == [7] and uploads.owners("s") == [9]

    def test_names_already_taken_by_the_set_are_not_given_again(self, staging):
        upload_id, _ = uploads.store("s7", 3, "report.pdf", b"b")
        taken = {"report.pdf"}
        assert uploads.descriptor("s7", 3, [upload_id], "k", taken=taken)["files"][0]["as"] == "report-2.pdf"
        assert taken == {"report.pdf", "report-2.pdf"}
