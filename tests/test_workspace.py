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
