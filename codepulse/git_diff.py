"""
Phase 3.1 — Git-diff based change detection (roadmap §37 item 3.1:
"Git-diff change detection (added/modified/deleted)"; §9 Incremental
Indexing Design: "Incremental indexing for a git repo uses `git diff
<last_commit>..<current_commit> --name-status` to get the file-level
change set directly -- cheaper and more precise than hashing every
file").

This module is a real, standalone primitive: given a local git working
tree and a previously-recorded commit SHA, determine exactly which
files were added, modified, or deleted since that commit. It has no
dependency on `IndexingEngine`, the database, or any other CodePulse
subsystem -- mirroring the established "top-level, cross-cutting
concern" placement of `codepulse/jobs.py`, `codepulse/rate_limit.py`,
and `codepulse/ingestion_safety.py`.

Scope, precisely -- deliberately NOT inferring later Phase 3 items:
  - Renames/moves are NOT detected as such. `git diff --no-renames` is
    used explicitly, so a renamed file is reported as a plain delete
    (old path) + add (new path) pair, exactly matching this item's own
    literal three-category framing ("added/modified/deleted"). Rename-
    aware re-keying of Symbol/Embedding rows across a path change is
    roadmap §37 item 3.2 ("Rename/move detection + symbol re-keying"),
    a distinct, later sprint item -- not built here.
  - Symbol-level `body_hash` comparison to skip unchanged embeddings
    (§37 item 3.3) is not implemented here; this module only determines
    which *files* changed, not which *symbols within* a changed file
    actually differ.
  - Call-graph confidence scoring (3.4) and local BFS incremental graph
    updates (3.5) are unrelated to this module.

Uses the system `git` binary via `subprocess` -- no new dependency was
added; `git` is already a prerequisite for this project to even be a
git repository, and no Python git library (GitPython etc.) is declared
anywhere in `pyproject.toml`.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field


class GitDiffError(Exception):
    """Raised when git-diff-based change detection cannot be performed --
    e.g. the path is not a git repository, the given commit no longer
    exists (a rebase/force-push invalidated it), or the `git` binary
    itself is unavailable. Callers are expected to catch this and fall
    back to a full (non-incremental) re-index, exactly matching §9's own
    "when a full re-index is still required" list."""


@dataclass(frozen=True)
class ChangeSet:
    """The result of `detect_file_changes`: three disjoint sets of
    repository-root-relative, forward-slash-normalized paths."""

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def changed_or_added(self) -> list[str]:
        """Every path that needs (re-)parsing -- added and modified,
        but not deleted (which needs cleanup, not parsing)."""
        return self.added + self.modified

    @property
    def total_changed(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)


def _run_git(args: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitDiffError(f"failed to invoke git in {cwd!r}: {e}") from e
    if result.returncode != 0:
        raise GitDiffError(
            f"git {' '.join(args)} failed in {cwd!r} (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def is_git_repository(path: str) -> bool:
    """Whether `path` is inside a git working tree. A real, current
    check via `git rev-parse` -- not a mere `.git` directory existence
    check, so a path inside a subdirectory of a repo (or a worktree)
    is correctly recognized too."""
    try:
        output = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=path)
    except GitDiffError:
        return False
    return output.strip() == "true"


def get_head_commit_sha(path: str) -> str | None:
    """The current `HEAD` commit SHA for the git repository at `path`,
    or `None` if `path` is not a git repository or has no commits yet
    (a freshly-`git init`'d, empty repo)."""
    if not is_git_repository(path):
        return None
    try:
        output = _run_git(["rev-parse", "HEAD"], cwd=path)
    except GitDiffError:
        return None
    sha = output.strip()
    return sha or None


def detect_file_changes(repo_path: str, since_commit: str) -> ChangeSet:
    """
    Returns the set of files added, modified, or deleted between
    `since_commit` and the current `HEAD`, using `git diff --no-renames
    --name-status` (§9's own literal recommendation).

    Raises `GitDiffError` if `repo_path` is not a git repository, if
    `since_commit` no longer resolves (e.g. history was rewritten), or
    if `git` itself fails for any other reason -- callers must catch
    this and fall back to a full re-index; this function never silently
    returns an empty/wrong result on failure.
    """
    if not is_git_repository(repo_path):
        raise GitDiffError(f"{repo_path!r} is not a git repository")

    output = _run_git(
        ["diff", "--no-renames", "--name-status", f"{since_commit}..HEAD"],
        cwd=repo_path,
    )

    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, rel_path = parts[0], parts[1]
        rel_path = rel_path.replace("\\", "/")
        # With --no-renames, git only ever reports A (added), M
        # (modified), D (deleted), or T (type change, e.g. a symlink
        # becoming a regular file) -- treated as "modified" here, since
        # it is neither a pure add nor a pure delete.
        if status.startswith("A"):
            added.append(rel_path)
        elif status.startswith("D"):
            deleted.append(rel_path)
        else:  # M, T, or any other single-letter status --no-renames can emit
            modified.append(rel_path)

    return ChangeSet(added=added, modified=modified, deleted=deleted)
