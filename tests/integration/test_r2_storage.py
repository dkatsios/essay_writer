"""Integration tests for RunStorage against a real Cloudflare R2 bucket.

Run with::

    uv run python -m pytest tests/integration/test_r2_storage.py -v
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.skipif(
    not pytest.importorskip("boto3", reason="boto3 not installed"),
    reason="skipped",
)


class TestR2ReadWrite:
    def test_write_and_read_text(self, r2_storage):
        r2_storage.write_text("hello.txt", "Hello, R2!")
        assert r2_storage.read_text("hello.txt") == "Hello, R2!"

    def test_write_and_read_bytes(self, r2_storage):
        data = b"\x00\x01\x02\xff" * 256
        r2_storage.write_bytes("binary.bin", data)
        assert r2_storage.read_bytes("binary.bin") == data

    def test_overwrite_replaces_content(self, r2_storage):
        r2_storage.write_text("file.txt", "version 1")
        r2_storage.write_text("file.txt", "version 2")
        assert r2_storage.read_text("file.txt") == "version 2"

    def test_nested_subpath(self, r2_storage):
        r2_storage.write_text("a/b/c/deep.txt", "deep content")
        assert r2_storage.read_text("a/b/c/deep.txt") == "deep content"

    def test_unicode_content(self, r2_storage):
        text = "Ελληνικά κείμενα 📝 — τεστ"
        r2_storage.write_text("unicode.txt", text)
        assert r2_storage.read_text("unicode.txt") == text


class TestR2Exists:
    def test_exists_true(self, r2_storage):
        r2_storage.write_text("present.txt", "here")
        assert r2_storage.exists("present.txt") is True

    def test_exists_false(self, r2_storage):
        assert r2_storage.exists("nonexistent.txt") is False


class TestR2FileSize:
    def test_file_size(self, r2_storage):
        content = "abcde"
        r2_storage.write_text("sized.txt", content)
        assert r2_storage.file_size("sized.txt") == len(content.encode())

    def test_file_size_missing_raises(self, r2_storage):
        with pytest.raises(FileNotFoundError):
            r2_storage.file_size("missing.txt")


class TestR2ReadMissing:
    def test_read_bytes_missing_raises(self, r2_storage):
        with pytest.raises(FileNotFoundError):
            r2_storage.read_bytes("no_such_file.bin")

    def test_read_text_missing_raises(self, r2_storage):
        with pytest.raises(FileNotFoundError):
            r2_storage.read_text("no_such_file.txt")


class TestR2ListFiles:
    def test_list_files_all(self, r2_storage):
        r2_storage.write_text("a.txt", "a")
        r2_storage.write_text("dir/b.txt", "b")
        r2_storage.write_text("dir/sub/c.txt", "c")

        files = r2_storage.list_files()
        assert files == ["a.txt", "dir/b.txt", "dir/sub/c.txt"]

    def test_list_files_with_prefix(self, r2_storage):
        r2_storage.write_text("sources/registry.json", "{}")
        r2_storage.write_text("sources/notes/s1.json", "{}")
        r2_storage.write_text("essay/draft.md", "draft")

        assert r2_storage.list_files("sources/") == [
            "sources/notes/s1.json",
            "sources/registry.json",
        ]

    def test_list_files_empty(self, r2_storage):
        assert r2_storage.list_files() == []


class TestR2ListDir:
    def test_list_dir_root(self, r2_storage):
        r2_storage.write_text("top.txt", "t")
        r2_storage.write_text("sub/nested.txt", "n")

        immediate = r2_storage.list_dir()
        assert "top.txt" in immediate
        # nested file should not appear at root level
        assert "sub/nested.txt" not in immediate

    def test_list_dir_subdir(self, r2_storage):
        r2_storage.write_text("sources/a.json", "{}")
        r2_storage.write_text("sources/b.json", "{}")
        r2_storage.write_text("sources/notes/c.json", "{}")

        files = r2_storage.list_dir("sources")
        assert sorted(files) == ["a.json", "b.json"]


class TestR2Delete:
    def test_delete_single(self, r2_storage):
        r2_storage.write_text("doomed.txt", "bye")
        assert r2_storage.exists("doomed.txt") is True
        r2_storage.delete("doomed.txt")
        assert r2_storage.exists("doomed.txt") is False

    def test_delete_nonexistent_is_silent(self, r2_storage):
        # S3 delete is idempotent — no error for missing keys
        r2_storage.delete("ghost.txt")

    def test_delete_all(self, r2_storage):
        r2_storage.write_text("a.txt", "a")
        r2_storage.write_text("b/c.txt", "c")
        r2_storage.write_text("b/d/e.txt", "e")

        count = r2_storage.delete_all()
        assert count == 3
        assert r2_storage.list_files() == []


class TestR2FullLifecycle:
    """End-to-end: simulate a mini pipeline run's artifact lifecycle."""

    def test_create_upload_download_verify_delete(self, r2_storage):
        # 1. Write artifacts like the pipeline would
        r2_storage.write_text("brief/assignment.json", '{"topic": "test"}')
        r2_storage.write_text("plan/plan.json", '{"sections": []}')
        r2_storage.write_text("essay/draft.md", "# Draft\n\nBody text here.")
        r2_storage.write_bytes("essay.docx", b"PK\x03\x04fake-docx-content")

        # 2. Verify all files are listed
        all_files = r2_storage.list_files()
        assert "brief/assignment.json" in all_files
        assert "plan/plan.json" in all_files
        assert "essay/draft.md" in all_files
        assert "essay.docx" in all_files

        # 3. Download and verify content
        assert r2_storage.read_text("brief/assignment.json") == '{"topic": "test"}'
        assert r2_storage.read_bytes("essay.docx").startswith(b"PK\x03\x04")

        # 4. Verify sizes
        assert r2_storage.file_size("essay/draft.md") == len(
            "# Draft\n\nBody text here.".encode()
        )

        # 5. Delete everything
        deleted = r2_storage.delete_all()
        assert deleted == 4
        assert r2_storage.list_files() == []
        assert r2_storage.exists("essay.docx") is False
