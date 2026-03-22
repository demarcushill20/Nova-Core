"""Enhanced tests for tools/files.py — critical file operation coverage.

Covers path safety, binary detection, error handling, and edge cases.
"""

import pytest

from tools.files import (
    dispatch_files_tool,
    list_glob,
    read_text,
    resolve_path,
    unified_diff,
    write_text,
)


class TestPathSafety:
    """Test path resolution and sandbox enforcement."""

    def test_resolve_path_absolute_within_sandbox(self, tmp_path):
        """Absolute path within sandbox should resolve correctly."""
        target = tmp_path / "subdir" / "file.txt"
        result = resolve_path(tmp_path, str(target))
        assert result == target

    def test_resolve_path_relative_to_sandbox(self, tmp_path):
        """Relative path should resolve relative to sandbox root."""
        result = resolve_path(tmp_path, "subdir/file.txt")
        assert result == tmp_path / "subdir" / "file.txt"

    def test_resolve_path_escapes_sandbox_raises(self, tmp_path):
        """Path escaping sandbox should raise ValueError."""
        with pytest.raises(ValueError, match="outside sandbox"):
            resolve_path(tmp_path, "../outside.txt")

        with pytest.raises(ValueError, match="outside sandbox"):
            resolve_path(tmp_path, str(tmp_path.parent / "outside.txt"))

    def test_resolve_path_symlink_escape_blocked(self, tmp_path):
        """Symlink escaping sandbox should be blocked."""
        # Create symlink pointing outside sandbox
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("outside")
        symlink = tmp_path / "link.txt"
        symlink.symlink_to(outside)

        with pytest.raises(ValueError, match="outside sandbox"):
            resolve_path(tmp_path, "link.txt")


class TestFileOperations:
    """Test core file read/write operations."""

    def test_read_text_nonexistent_file(self, tmp_path):
        """Reading nonexistent file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            read_text(tmp_path / "missing.txt")

    def test_read_text_binary_file_raises(self, tmp_path):
        """Reading binary file should raise ValueError."""
        binary_file = tmp_path / "binary.bin"
        binary_file.write_bytes(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08")

        with pytest.raises(ValueError, match="Binary file detected"):
            read_text(binary_file)

    def test_write_text_create_dirs(self, tmp_path):
        """Writing with create_dirs=True should create parent directories."""
        target = tmp_path / "deep" / "nested" / "file.txt"
        content = "nested file content"

        bytes_written = write_text(target, content, create_dirs=True)

        assert target.exists()
        assert target.read_text() == content
        assert bytes_written == len(content.encode())

    def test_write_text_no_create_dirs_missing_parent(self, tmp_path):
        """Writing without create_dirs should raise if parent missing."""
        target = tmp_path / "missing" / "file.txt"

        with pytest.raises(FileNotFoundError):
            write_text(target, "content", create_dirs=False)

    def test_list_glob_many_files(self, tmp_path):
        """List with many files should work correctly."""
        # Create several files
        for i in range(50):  # Reasonable number for testing
            (tmp_path / f"file_{i:04d}.txt").write_text(f"content {i}")

        entries = list_glob(tmp_path, "*.txt")
        assert len(entries) == 50
        assert all("file_" in entry for entry in entries)

    def test_unified_diff_identical_content(self, tmp_path):
        """Diff of identical content should return empty string."""
        content = "line 1\nline 2\nline 3"

        diff = unified_diff(content, content)
        assert diff == ""

    def test_unified_diff_different_content(self, tmp_path):
        """Diff of different content should show differences."""
        content1 = "line 1\nline 2\nline 3"
        content2 = "line 1\nmodified line 2\nline 3"

        diff = unified_diff(content1, content2)
        assert "-line 2" in diff
        assert "+modified line 2" in diff


class TestFilesToolDispatcher:
    """Test the tool dispatcher interface."""

    def test_dispatch_files_tool_invalid_tool(self, tmp_path):
        """Invalid tool name should raise ValueError."""
        registry = {"sandbox_root": tmp_path}

        with pytest.raises(ValueError, match="Unknown files tool"):
            dispatch_files_tool("invalid_tool", {}, registry)

    def test_dispatch_files_tool_read_success(self, tmp_path):
        """Successful read operation through dispatcher."""
        test_file = tmp_path / "test.txt"
        content = "test content"
        test_file.write_text(content)

        registry = {"sandbox_root": tmp_path}
        args = {"path": "test.txt"}

        result = dispatch_files_tool("files.read", args, registry)

        assert "path" in result
        assert result["content"] == content
        assert result["lines"] > 0

    def test_dispatch_files_tool_write_success(self, tmp_path):
        """Successful write operation through dispatcher."""
        registry = {"sandbox_root": tmp_path}
        args = {"path": "output.txt", "content": "new content"}

        result = dispatch_files_tool("files.write", args, registry)

        assert "path" in result
        assert result["bytes"] > 0
        assert (tmp_path / "output.txt").read_text() == "new content"

    def test_dispatch_files_tool_list_success(self, tmp_path):
        """Successful list operation through dispatcher."""
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")

        registry = {"sandbox_root": tmp_path}
        args = {"pattern": "*.txt"}

        result = dispatch_files_tool("files.list", args, registry)

        assert result["count"] == 2
        assert len(result["entries"]) == 2
        assert any("file1.txt" in entry for entry in result["entries"])
        assert any("file2.txt" in entry for entry in result["entries"])

    def test_dispatch_files_tool_diff_success(self, tmp_path):
        """Successful diff operation through dispatcher."""
        file1 = tmp_path / "file1.txt"
        file2 = tmp_path / "file2.txt"
        file1.write_text("original\ncontent")
        file2.write_text("modified\ncontent")

        registry = {"sandbox_root": tmp_path}
        args = {"path_a": "file1.txt", "path_b": "file2.txt"}

        result = dispatch_files_tool("files.diff", args, registry)

        assert "diff" in result
        assert result["changed"] is True
        assert "-original" in result["diff"]
        assert "+modified" in result["diff"]


class TestErrorHandling:
    """Test comprehensive error handling."""

    def test_read_text_permission_denied(self, tmp_path):
        """Test handling of permission denied errors."""
        test_file = tmp_path / "restricted.txt"
        test_file.write_text("content")
        test_file.chmod(0o000)  # Remove all permissions

        try:
            with pytest.raises(PermissionError):
                read_text(test_file)
        finally:
            test_file.chmod(0o644)  # Restore permissions for cleanup

    def test_write_text_readonly_file(self, tmp_path):
        """Test writing to read-only file raises error."""
        readonly_file = tmp_path / "readonly.txt"
        readonly_file.write_text("original")
        readonly_file.chmod(0o444)  # Make read-only

        try:
            with pytest.raises(PermissionError):
                write_text(readonly_file, "new content")
        finally:
            readonly_file.chmod(0o644)  # Restore for cleanup

    def test_list_glob_permission_denied_directory(self, tmp_path):
        """Test listing directory without permission."""
        restricted_dir = tmp_path / "restricted"
        restricted_dir.mkdir()
        (restricted_dir / "file.txt").write_text("content")
        restricted_dir.chmod(0o000)  # Remove all permissions

        try:
            # Should handle permission error gracefully
            entries = list_glob(restricted_dir, "*")
            # Might be empty list or raise PermissionError depending on implementation
            assert isinstance(entries, list)
        except PermissionError:
            # This is also acceptable behavior
            pass
        finally:
            restricted_dir.chmod(0o755)  # Restore for cleanup
