"""
Phase 3.1/3.2 — Git-diff based change detection, including rename/move
detection (roadmap §37 item 3.1: "Git-diff change detection (added/
modified/deleted)"; item 3.2: "Rename/move detection + symbol
re-keying"; §9 Incremental Indexing Design: "Incremental indexing for a
git repo uses `git diff <last_commit>..<current_commit> --name-status`
to get the file-level change set directly" and "for git repos, use
git's own rename detection (`git diff -M`) to map `(old_path ->
new_path)`").

This module is a real, standalone primitive: given a local git working
tree and a previously-recorded commit SHA, determine exactly which
files were added, modified, deleted, or renamed/moved since that
commit. It has no dependency on `IndexingEngine`, the database, or any
other CodePulse subsystem -- mirroring the established "top-level,
cross-cutting concern" placement of `codepulse/jobs.py`,
`codepulse/rate_limit.py`, and `codepulse/ingestion_safety.py`.

Rename/move detection (Phase 3.2, new in this module):
  - `-M` is passed explicitly to `git diff` (git's own rename-detection
    flag, per §9's own literal text), rather than relying on ambient
    `diff.renames` config or git's version-dependent default -- so
    behavior is deterministic across environments. This uses git's own
    default similarity threshold (50%): a path pair git itself judges
    at least 50% similar is reported as a rename (`R<NNN>`, e.g. `R100`
    for a byte-identical move, `R087` for a move with a fairly small
    edit); below that threshold, git reports its own plain delete+add
    instead, and this module does not override or second-guess that
    judgment -- matching the roadmap's own risk-register mitigation
    ("git rename detection... with fallback to delete+recreate rather
    than a risky guess").
  - Copy detection (`-C`) is deliberately NOT enabled -- the roadmap
    only names rename/move detection, not copy detection, and enabling
    it would require a different, unrequested handling path (a copy
    keeps the original file too, unlike a rename).
  - `ChangeSet.renamed` is a list of `(old_path, new_path)` pairs. It is
    disjoint from `added`/`modified`/`deleted` -- once rename detection
    is active, git itself never double-reports a renamed path as a
    separate delete+add.

Scope, precisely -- deliberately NOT inferring later Phase 3 items:
  - This module identifies the rename mapping only. Whether/how to
    re-key the corresponding `File`/`Symbol`/`Embedding` DB rows and
    search-index doc_ids in place (rather than delete+recreate) is
    `IndexingEngine`'s job (`codepulse/indexer/engine.py`), not this
    module's.
  - Symbol-level `body_hash` comparison to skip unchanged embeddings
    within a changed (or renamed-with-modification) file (§37 item 3.3)
    is not implemented here; this module only determines which *files*
    changed/moved, not which *symbols within* a file actually differ.
  - Call-graph confidence scoring (3.4) and local BFS incremental graph
    updates (3.5) -- including correcting *other* files' call-graph
    edges that reference a renamed file's old path -- are unrelated to
    this module.

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
    """The result of `detect_file_changes`: disjoint sets of
    repository-root-relative, forward-slash-normalized paths, plus
    (Phase 3.2) a list of rename/move `(old_path, new_path)` pairs."""

    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed_or_added(self) -> list[str]:
        """Every path that needs (re-)parsing -- added and modified
        files, plus a rename/move's NEW path (its content, even if
        byte-identical to before the move, must still be read and
        parsed at least once under its new path so every in-memory/
        durable structure that keys off `file_path` -- chunks, doc_ids,
        the call graph -- reflects the new path). Does not include
        `deleted` (needs cleanup, not parsing) or a rename's OLD path
        (superseded, not itself parsed)."""
        return self.added + self.modified + [new for _, new in self.renamed]

    @property
    def total_changed(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted) + len(self.renamed)


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
    Returns the set of files added, modified, deleted, or renamed/moved
    between `since_commit` and the current `HEAD`, using `git diff -M
    --name-status` (§9's own literal recommendation for rename
    detection, combined with its own literal `--name-status` recipe for
    the base change-detection mechanism).

    Raises `GitDiffError` if `repo_path` is not a git repository, if
    `since_commit` no longer resolves (e.g. history was rewritten), or
    if `git` itself fails for any other reason -- callers must catch
    this and fall back to a full re-index; this function never silently
    returns an empty/wrong result on failure.
    """
    if not is_git_repository(repo_path):
        raise GitDiffError(f"{repo_path!r} is not a git repository")

    output = _run_git(
        ["diff", "-M", "--name-status", f"{since_commit}..HEAD"],
        cwd=repo_path,
    )

    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: list[tuple[str, str]] = []

    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]

        if status.startswith("R"):
            # Rename/move: "R<similarity>\t<old_path>\t<new_path>" --
            # three tab-separated fields, unlike every other status.
            if len(parts) < 3:
                continue
            old_path = parts[1].replace("\\", "/")
            new_path = parts[2].replace("\\", "/")
            renamed.append((old_path, new_path))
            continue

        rel_path = parts[1].replace("\\", "/")
        if status.startswith("A"):
            added.append(rel_path)
        elif status.startswith("D"):
            deleted.append(rel_path)
        else:  # M, T, or any other single-letter status
            modified.append(rel_path)

    return ChangeSet(added=added, modified=modified, deleted=deleted, renamed=renamed)
