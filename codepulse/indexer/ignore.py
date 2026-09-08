"""
Phase 0.8 -- .gitignore / .codepulseignore discovery and ignore-rule handling.

Loads a repository's root-level `.gitignore` and `.codepulseignore` files
(if present) and answers whether a given repository-relative path should be
excluded from indexing, before that path is ever read or parsed.

Semantics (per CodePulse_Strategy_and_Roadmap.md §8/§16 and roadmap sprint
item 0.8 / backlog item B6 -- "Ignored files never appear in the index"):

  - `.gitignore` is respected using standard gitignore pattern syntax
    (§16: "Respect `.gitignore` during file discovery").
  - `.codepulseignore` uses the "same syntax" (§16) for CodePulse-specific
    exclusions "beyond git's own" -- i.e. ADDITIVE to `.gitignore`, never a
    replacement/override of it.
  - The two files are evaluated INDEPENDENTLY, each with its own negation
    patterns resolved only within itself, and a path is excluded if EITHER
    file's ruleset matches it. A negation (`!pattern`) in one file cannot
    reach across and un-ignore something the other file matches -- this
    keeps ".codepulseignore extends .gitignore" unambiguous instead of
    inventing cross-file negation semantics the roadmap doesn't specify.
  - Only the REPOSITORY-ROOT `.gitignore`/`.codepulseignore` are read.
    Nested, per-directory ignore files (as real git supports) are NOT
    implemented here -- the roadmap doesn't call for it, and it's a
    materially larger feature than "respect the repository's ignore
    rules." Documented limitation (see CODEPULSE_PROGRESS.md).
  - An absent ignore file is treated as an empty ruleset -- not an error.
  - If a *present* ignore file cannot be read, or contains a pattern
    `pathspec` cannot parse, that file's ENTIRE ruleset is treated as
    absent (empty) for this indexing run, and a warning is logged naming
    the file and the error. Indexing proceeds rather than aborting, but
    the failure is not silently swallowed, and a malformed file never
    causes MORE or FEWER exclusions than "this file contributed nothing."
  - The ignore files themselves (`.gitignore`, `.codepulseignore`) are not
    given any special exemption from these rules, but in practice they
    already never become indexed `FileModel`/`SymbolModel` rows regardless
    -- no `LanguageParser` recognizes them (see `parser/factory.py`), and
    `IndexingEngine.index_directory` only persists files a parser accepts.
  - No file-size or file-type caps are implemented here (§8 lists them
    alongside ignore-file handling, but they're a distinct, not-yet-built
    concern -- out of this milestone's scope).
"""
from __future__ import annotations

import logging
import os

import pathspec

logger = logging.getLogger(__name__)

GITIGNORE_FILENAME = ".gitignore"
CODEPULSEIGNORE_FILENAME = ".codepulseignore"


def _load_spec(repo_path: str, filename: str) -> pathspec.PathSpec | None:
    """
    Loads one root-level ignore file into a `PathSpec` using gitwildmatch
    syntax. Returns None if the file doesn't exist, can't be read, or
    contains a pattern that can't be parsed (logging a warning in the
    latter two cases) -- callers treat None as "this file contributes no
    rules," never as an error to propagate.
    """
    file_path = os.path.join(repo_path, filename)
    if not os.path.isfile(file_path):
        return None

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as e:
        logger.warning(
            "Could not read %s: %s -- its rules are not applied for this indexing run.",
            file_path,
            e,
        )
        return None

    try:
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except Exception as e:
        logger.warning(
            "Could not parse %s (malformed pattern): %s -- its rules are not applied "
            "for this indexing run.",
            file_path,
            e,
        )
        return None


class RepositoryIgnoreRules:
    """
    Combined `.gitignore` + `.codepulseignore` rules for one repository
    root. Loaded ONCE per indexing operation (see module docstring for
    precedence/combination semantics) and reused for every discovered path
    -- callers should construct one instance per `index_directory()` call,
    not re-parse the ignore files per file/directory.
    """

    def __init__(self, repo_path: str) -> None:
        self.repo_path = os.path.abspath(repo_path)
        self._gitignore_spec = _load_spec(self.repo_path, GITIGNORE_FILENAME)
        self._codepulseignore_spec = _load_spec(self.repo_path, CODEPULSEIGNORE_FILENAME)

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        """
        `rel_path` must be repository-root-relative, using forward slashes
        (the same path representation the rest of the indexing pipeline
        uses for `FileParseResult`/`Symbol`/DB rows). A trailing slash is
        added automatically for directories, matching gitwildmatch's
        convention for directory-only patterns (e.g. `build/`).
        """
        rel_path = rel_path.replace("\\", "/").lstrip("/")
        match_path = rel_path.rstrip("/") + "/" if is_dir else rel_path

        if self._gitignore_spec is not None and self._gitignore_spec.match_file(match_path):
            return True
        if self._codepulseignore_spec is not None and self._codepulseignore_spec.match_file(match_path):
            return True
        return False
