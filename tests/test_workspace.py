import os
from pathlib import Path

import pytest

from code_runner.workspace import WorkspaceManager, WorkspaceError


@pytest.fixture
def wm(tmp_path):
    return WorkspaceManager(root=tmp_path)


def test_session_dir_created_lazily(wm, tmp_path):
    p = wm.resolve_path("sess1", "out.csv")
    assert p.parent == tmp_path / "sess1"
    assert (tmp_path / "sess1").is_dir()


def test_session_dir_not_created_until_used(wm, tmp_path):
    assert not (tmp_path / "sess1").exists()


def test_rejects_absolute_path(wm):
    with pytest.raises(WorkspaceError, match="absolute"):
        wm.resolve_path("sess1", "/etc/passwd")


def test_rejects_parent_traversal(wm):
    with pytest.raises(WorkspaceError, match="traversal"):
        wm.resolve_path("sess1", "../../etc/passwd")


def test_rejects_empty_path(wm):
    with pytest.raises(WorkspaceError):
        wm.resolve_path("sess1", "")


def test_rejects_symlink_escape(wm, tmp_path):
    wm.resolve_path("sess1", "ok.txt")  # create dir
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = tmp_path / "sess1" / "evil"
    os.symlink(outside, link)
    with pytest.raises(WorkspaceError, match="symlink|outside"):
        wm.resolve_path("sess1", "evil")


def test_nested_subdir_path_returned(wm, tmp_path):
    p = wm.resolve_path("sess1", "sub/inner/out.csv")
    assert p == tmp_path / "sess1" / "sub" / "inner" / "out.csv"


def test_cleanup_session_removes_dir(wm, tmp_path):
    wm.resolve_path("sess1", "f.txt")
    assert (tmp_path / "sess1").is_dir()
    wm.cleanup_session("sess1")
    assert not (tmp_path / "sess1").exists()


def test_cleanup_session_idempotent(wm):
    wm.cleanup_session("never-existed")  # no error


def test_session_id_must_be_safe(wm):
    with pytest.raises(WorkspaceError, match="session_id"):
        wm.resolve_path("../escape", "f.txt")
    with pytest.raises(WorkspaceError, match="session_id"):
        wm.resolve_path("a/b", "f.txt")


def test_session_id_rejects_dot_and_dotdot(wm):
    for bad in (".", "..", ".hidden", "-leading-dash"):
        with pytest.raises(WorkspaceError, match="session_id"):
            wm.resolve_path(bad, "f.txt")


def test_resolve_path_returns_value_for_nul_byte_path(wm):
    # We don't add an explicit NUL-byte check in resolve_path — the
    # underlying os.path.realpath / open raise ValueError. Pin the
    # current behavior so a future refactor flags any change.
    with pytest.raises(ValueError, match="null"):
        wm.resolve_path("sess1", "a\x00b")


from code_runner.workspace import safe_open, DEFAULT_WRITE_CAP


def test_safe_open_write_text(wm):
    with safe_open(wm, "sess1", "out.txt", "w") as f:
        f.write("hello")
    p = wm.resolve_path("sess1", "out.txt")
    assert p.read_text() == "hello"


def test_safe_open_read_after_write(wm):
    with safe_open(wm, "sess1", "out.txt", "w") as f:
        f.write("hi")
    with safe_open(wm, "sess1", "out.txt", "r") as f:
        assert f.read() == "hi"


def test_safe_open_binary(wm):
    with safe_open(wm, "sess1", "blob.bin", "wb") as f:
        f.write(b"\x00\x01\x02")
    with safe_open(wm, "sess1", "blob.bin", "rb") as f:
        assert f.read() == b"\x00\x01\x02"


def test_safe_open_append(wm):
    with safe_open(wm, "sess1", "log.txt", "w") as f:
        f.write("a")
    with safe_open(wm, "sess1", "log.txt", "a") as f:
        f.write("b")
    p = wm.resolve_path("sess1", "log.txt")
    assert p.read_text() == "ab"


def test_safe_open_rejects_other_modes(wm):
    for mode in ("x", "r+", "w+", "rt+"):
        with pytest.raises(WorkspaceError, match="mode"):
            safe_open(wm, "sess1", "f.txt", mode)


def test_safe_open_creates_parent_dirs(wm):
    with safe_open(wm, "sess1", "deep/nested/f.txt", "w") as f:
        f.write("x")
    assert wm.resolve_path("sess1", "deep/nested/f.txt").read_text() == "x"


def test_safe_open_write_cap_enforced(wm):
    with pytest.raises(WorkspaceError, match="cap"):
        with safe_open(wm, "sess1", "big.bin", "wb", max_bytes=10) as f:
            f.write(b"x" * 11)


def test_safe_open_write_cap_across_multiple_writes(wm):
    with safe_open(wm, "sess1", "big.bin", "wb", max_bytes=10) as f:
        f.write(b"x" * 5)
        f.write(b"y" * 5)  # exactly at cap, fine
        with pytest.raises(WorkspaceError, match="cap"):
            f.write(b"z")


def test_safe_open_default_cap_is_50mb():
    assert DEFAULT_WRITE_CAP == 50 * 1024 * 1024


def test_safe_open_traversal_rejected(wm):
    with pytest.raises(WorkspaceError, match="traversal"):
        safe_open(wm, "sess1", "../escape.txt", "w")
