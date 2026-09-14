"""
Phase 3.1 — Git-diff based change detection (roadmap §37 item 3.1).

Exercises `codepulse/git_diff.py` directly against real, temporary git
repositories (created fresh under `tmp_path` via the real `git` binary --
never the production repository, never a mocked git implementation).
"""
from __future__ import annotations

import subprocess

import pytest

from codepulse.git_diff import (
    ChangeSet,
    GitDiffError,
    detect_file_changes,
    get_head_commit_sha,
    is_git_repository,
)


def _git(repo_path, *args) -> None:
    subprocess.run(["git", *args], cwd=str(repo_path), check=True, capture_output=True, text=True)


def _init_repo(repo_path) -> None:
    repo_path.mkdir(exist_ok=True)
    _git(repo_path, "init", "-q")
    # Local, repo-scoped identity -- does not depend on (or alter) any
    # global/ambient git config on the machine running these tests.
    _git(repo_path, "config", "user.email", "test@codepulse.local")
    _git(repo_path, "config", "user.name", "CodePulse Test")


def _commit_all(repo_path, message: str) -> str:
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", message)
    return get_head_commit_sha(str(repo_path))


# --- is_git_repository -------------------------------------------------------

def test_is_git_repository_true_for_a_real_git_repo(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert is_git_repository(str(repo)) is True


def test_is_git_repository_false_for_a_plain_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_git_repository(str(plain)) is False


def test_is_git_repository_false_for_a_nonexistent_path(tmp_path):
    assert is_git_repository(str(tmp_path / "does_not_exist")) is False


# --- get_head_commit_sha -----------------------------------------------------

def test_get_head_commit_sha_none_for_non_git_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert get_head_commit_sha(str(plain)) is None


def test_get_head_commit_sha_none_for_a_freshly_initialized_empty_repo(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert get_head_commit_sha(str(repo)) is None


def test_get_head_commit_sha_returns_a_real_sha_after_a_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    sha = _commit_all(repo, "initial")
    assert sha is not None
    assert len(sha) == 40  # a real, full git SHA-1 hex string
    assert get_head_commit_sha(str(repo)) == sha


# --- detect_file_changes -----------------------------------------------------

def test_detect_file_changes_raises_for_a_non_git_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitDiffError):
        detect_file_changes(str(plain), since_commit="deadbeef")


def test_detect_file_changes_raises_for_an_unresolvable_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "initial")
    with pytest.raises(GitDiffError):
        detect_file_changes(str(repo), since_commit="0000000000000000000000000000000000dead")


def test_detect_file_changes_detects_an_added_file(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    sha1 = _commit_all(repo, "initial")

    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")
    _commit_all(repo, "add b")

    changes = detect_file_changes(str(repo), since_commit=sha1)
    assert changes.added == ["b.py"]
    assert changes.modified == []
    assert changes.deleted == []
    assert changes.changed_or_added == ["b.py"]
    assert changes.total_changed == 1


def test_detect_file_changes_detects_a_modified_file(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    sha1 = _commit_all(repo, "initial")

    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _commit_all(repo, "modify a")

    changes = detect_file_changes(str(repo), since_commit=sha1)
    assert changes.added == []
    assert changes.modified == ["a.py"]
    assert changes.deleted == []


def test_detect_file_changes_detects_a_deleted_file(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")
    sha1 = _commit_all(repo, "initial")

    (repo / "b.py").unlink()
    _commit_all(repo, "delete b")

    changes = detect_file_changes(str(repo), since_commit=sha1)
    assert changes.added == []
    assert changes.modified == []
    assert changes.deleted == ["b.py"]
    assert changes.changed_or_added == []


def test_detect_file_changes_detects_added_modified_and_deleted_together(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "keep.py").write_text("k = 1\n", encoding="utf-8")
    (repo / "change_me.py").write_text("c = 1\n", encoding="utf-8")
    (repo / "remove_me.py").write_text("r = 1\n", encoding="utf-8")
    sha1 = _commit_all(repo, "initial")

    (repo / "change_me.py").write_text("c = 2\n", encoding="utf-8")
    (repo / "remove_me.py").unlink()
    (repo / "new_file.py").write_text("n = 1\n", encoding="utf-8")
    _commit_all(repo, "mixed changes")

    changes = detect_file_changes(str(repo), since_commit=sha1)
    assert changes.added == ["new_file.py"]
    assert changes.modified == ["change_me.py"]
    assert changes.deleted == ["remove_me.py"]
    assert "keep.py" not in (changes.added + changes.modified + changes.deleted)


def test_detect_file_changes_with_no_changes_returns_empty_changeset(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    sha1 = _commit_all(repo, "initial")

    changes = detect_file_changes(str(repo), since_commit=sha1)
    assert changes == ChangeSet(added=[], modified=[], deleted=[])
    assert changes.total_changed == 0


def test_detect_file_changes_a_rename_is_reported_as_delete_plus_add_not_a_rename(tmp_path):
    """Phase 3.1's own explicit scope boundary: renames are NOT detected
    as such (that is roadmap item 3.2, a distinct later milestone) --
    `--no-renames` forces git to report a rename as a plain delete of
    the old path and add of the new path."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "old_name.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    sha1 = _commit_all(repo, "initial")

    (repo / "old_name.py").rename(repo / "new_name.py")
    _commit_all(repo, "rename")

    changes = detect_file_changes(str(repo), since_commit=sha1)
    assert changes.deleted == ["old_name.py"]
    assert changes.added == ["new_name.py"]
    assert changes.modified == []
