"""
A full re-index removes the index rows of files that are no longer indexable
(CODEPULSE_FUTURE_CHANGES.md item 16; found while verifying item 15).

`_persist_to_db` upserts the current files but never removed the `files` /
`symbols` / `code_embeddings` rows of files that had since been deleted,
ignored or become unindexable; only the INCREMENTAL path purged them
(`_purge_stale_entries`). A full run happens for non-git repositories, for a
first run and after every server restart, so in practice deleted files
lingered: their symbols came back in keyword search after a restart, in the
restored symbol map (autocomplete, graph score), and inflated `file_count` /
`symbol_count`. Cached parse results of deleted files could also be merged
back into a later incremental run's graphs.

Rule: after a full run's parse loop, every stored file of the repository that
this run did not parse is purged, EXCEPT:
  - a file whose parse raised this run (a transient failure must not delete
    previously good rows; the next successful parse replaces them);
  - everything, when this run parsed NO files at all (an empty or unmounted
    directory must not wipe an index).
Incremental runs are unchanged (they already purge their own deletions).

Temporary databases only; Restart Model B as in test_restart_idempotency.py.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from codepulse.config import CodePulseConfig, reset_settings, set_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import (
    CallEdge,
    EmbeddingModel,
    FileModel,
    ModuleEdge,
    RepositoryModel,
    StorageManager,
    SymbolModel,
)
from codepulse.symbol_identity import symbol_doc_id


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture(scope="module")
def shared_embedder():
    return CodeEmbedder()


KEEP = "from other import helper\n\n\ndef keep():\n    \"\"\"Stays.\"\"\"\n    return helper()\n"
OTHER = "def helper():\n    return 1\n"
DOOMED_LINE = 4  # `def doomed` is on line 4 of DOOMED
DOOMED = "from other import helper\n\n\ndef doomed():\n    \"\"\"Goes away.\"\"\"\n    return helper()\n"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path, name, files, git=False):
    repo = tmp_path / name
    repo.mkdir()
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if git:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@codepulse.local")
        _git(repo, "config", "user.name", "T")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _db_url(tmp_path, name):
    return f"sqlite:///{str(tmp_path / name).replace(os.sep, '/')}"


def _new_engine(db_url, embedder):
    return IndexingEngine(storage=StorageManager(db_url=db_url), embedder=embedder)


def _restart(engine, db_url, embedder):
    engine.storage.engine.dispose()
    return _new_engine(db_url, embedder)


def _index(engine, repo):
    return engine.index_directory(str(repo), repo_name=repo.name, repo_url=str(repo))


def _db_state(engine, repo_id):
    session = engine.storage.get_session()
    try:
        files = sorted(f.path for f in session.query(FileModel).filter(FileModel.repo_id == repo_id))
        symbols = sorted(
            s.name
            for s in session.query(SymbolModel).join(FileModel).filter(FileModel.repo_id == repo_id)
        )
        embeddings = (
            session.query(EmbeddingModel)
            .join(SymbolModel, EmbeddingModel.symbol_id == SymbolModel.id)
            .join(FileModel, SymbolModel.file_id == FileModel.id)
            .filter(FileModel.repo_id == repo_id)
            .count()
        )
        repo = session.get(RepositoryModel, repo_id)
        return {
            "files": files,
            "symbols": symbols,
            "embeddings": embeddings,
            "file_count": repo.file_count,
            "symbol_count": repo.symbol_count,
        }
    finally:
        session.close()


def _hits(engine, repo_id, query):
    return [r.symbol_name for r in engine.search(query, limit=10, search_type="keyword", repository_id=repo_id)]


# --- deleted files ---------------------------------------------------------

def test_a_deleted_file_is_removed_from_every_index_on_the_next_full_run(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r1", {"keep.py": KEEP, "other.py": OTHER, "gone.py": DOOMED})
    engine = _new_engine(_db_url(tmp_path, "deleted.db"), shared_embedder)
    repo_id = _index(engine, repo)["repository_id"]
    assert "doomed" in _db_state(engine, repo_id)["symbols"]
    embeddings_before = _db_state(engine, repo_id)["embeddings"]

    (repo / "gone.py").unlink()
    result = _index(engine, repo)  # not a git repo -> a full run

    assert result["incremental"] is False
    state = _db_state(engine, repo_id)
    assert state["files"] == ["keep.py", "other.py"]
    assert "doomed" not in state["symbols"]
    assert state["embeddings"] < embeddings_before, "the deleted file's embeddings must go too"
    assert (state["file_count"], state["symbol_count"]) == (2, len(state["symbols"])), "counts must reflect the real rows"
    assert _hits(engine, repo_id, "doomed") == []
    assert symbol_doc_id(repo_id, "gone.py", "doomed", DOOMED_LINE) not in engine.symbols_by_id
    assert f"{repo_id}::gone.py" not in engine.parse_results


def test_the_deleted_file_stays_gone_after_a_restart(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r2", {"keep.py": KEEP, "other.py": OTHER, "gone.py": DOOMED})
    db_url = _db_url(tmp_path, "restart.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = _index(engine1, repo)["repository_id"]
    (repo / "gone.py").unlink()
    _index(engine1, repo)

    engine2 = _restart(engine1, db_url, shared_embedder)

    assert _hits(engine2, repo_id, "doomed") == []
    assert not any("gone.py" in doc_id for doc_id in engine2.symbols_by_id)
    assert "gone.py" not in engine2.analyze_module_structure(repo_id).files
    assert not [e for e in engine2.call_graphs[repo_id].edges if "gone.py" in (e.source_file, e.target_file or "")]


def test_a_full_run_forced_by_a_restart_purges_files_deleted_while_the_server_was_down(tmp_path, shared_embedder):
    """The real-world path: git repo, file deleted and committed, server restarted -> the
    next run is full (no parse cache), which used to leave the deleted file's rows."""
    repo = _make_repo(tmp_path, "r3", {"keep.py": KEEP, "other.py": OTHER, "gone.py": DOOMED}, git=True)
    db_url = _db_url(tmp_path, "git_restart.db")
    engine1 = _new_engine(db_url, shared_embedder)
    repo_id = _index(engine1, repo)["repository_id"]
    (repo / "gone.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete gone.py")

    engine2 = _restart(engine1, db_url, shared_embedder)
    result = _index(engine2, repo)

    assert result["incremental"] is False
    assert _db_state(engine2, repo_id)["files"] == ["keep.py", "other.py"]
    assert _hits(engine2, repo_id, "doomed") == []


# --- files that stop being indexable --------------------------------------

def test_a_file_that_becomes_ignored_leaves_the_index(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r4", {"keep.py": KEEP, "other.py": OTHER, "gen/generated.py": DOOMED})
    engine = _new_engine(_db_url(tmp_path, "ignored.db"), shared_embedder)
    repo_id = _index(engine, repo)["repository_id"]
    assert "gen/generated.py" in _db_state(engine, repo_id)["files"]

    (repo / ".codepulseignore").write_text("gen/\n", encoding="utf-8")
    _index(engine, repo)

    assert _db_state(engine, repo_id)["files"] == ["keep.py", "other.py"]
    assert _hits(engine, repo_id, "doomed") == []


def test_a_file_that_becomes_oversized_leaves_the_index(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r5", {"keep.py": KEEP, "other.py": OTHER, "big.py": DOOMED})
    engine = _new_engine(_db_url(tmp_path, "oversized.db"), shared_embedder)
    repo_id = _index(engine, repo)["repository_id"]
    assert "big.py" in _db_state(engine, repo_id)["files"]

    set_settings(CodePulseConfig(ingestion_max_file_size_bytes=len(DOOMED.encode()) - 1))
    _index(engine, repo)

    assert "big.py" not in _db_state(engine, repo_id)["files"]


# --- safety rails ----------------------------------------------------------

def test_a_file_that_fails_to_parse_keeps_its_previous_rows(tmp_path, shared_embedder, monkeypatch):
    repo = _make_repo(tmp_path, "r6", {"keep.py": KEEP, "other.py": OTHER, "flaky.py": DOOMED})
    engine = _new_engine(_db_url(tmp_path, "flaky.db"), shared_embedder)
    repo_id = _index(engine, repo)["repository_id"]
    before = _db_state(engine, repo_id)

    from codepulse.parser import factory

    real_get_parser = factory.get_parser_for_file

    class _Boom:
        def parse_file(self, rel_path, source):
            raise RuntimeError("transient parse failure")

    def flaky_parser(path):
        return _Boom() if path.endswith("flaky.py") else real_get_parser(path)

    monkeypatch.setattr("codepulse.indexer.engine.get_parser_for_file", flaky_parser)
    _index(engine, repo)

    after = _db_state(engine, repo_id)
    assert "flaky.py" in after["files"] and "doomed" in after["symbols"], "a transient failure must not delete good rows"
    assert after["files"] == before["files"]


def test_a_run_that_parses_no_files_never_wipes_the_index(tmp_path, shared_embedder):
    """An empty or unmounted directory must not be read as 'everything was deleted'."""
    repo = _make_repo(tmp_path, "r7", {"keep.py": KEEP, "other.py": OTHER})
    engine = _new_engine(_db_url(tmp_path, "empty_dir.db"), shared_embedder)
    repo_id = _index(engine, repo)["repository_id"]
    before = _db_state(engine, repo_id)

    (repo / "keep.py").unlink()
    (repo / "other.py").unlink()
    result = _index(engine, repo)

    assert result["total_files"] == 0
    assert _db_state(engine, repo_id)["files"] == before["files"]


def test_purging_one_repository_never_touches_another(tmp_path, shared_embedder):
    repo_a = _make_repo(tmp_path, "ra", {"keep.py": KEEP, "other.py": OTHER, "gone.py": DOOMED})
    repo_b = _make_repo(tmp_path, "rb", {"keep.py": KEEP, "other.py": OTHER, "gone.py": DOOMED})
    engine = _new_engine(_db_url(tmp_path, "isolation.db"), shared_embedder)
    id_a = _index(engine, repo_a)["repository_id"]
    id_b = _index(engine, repo_b)["repository_id"]
    b_before = _db_state(engine, id_b)

    (repo_a / "gone.py").unlink()
    _index(engine, repo_a)

    assert _db_state(engine, id_a)["files"] == ["keep.py", "other.py"]
    assert _db_state(engine, id_b) == b_before
    assert _hits(engine, id_b, "doomed") == ["doomed"]
    assert symbol_doc_id(id_b, "gone.py", "doomed", DOOMED_LINE) in engine.symbols_by_id


def test_the_derived_graph_rows_of_a_deleted_file_are_gone_too(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r8", {"keep.py": KEEP, "other.py": OTHER, "gone.py": DOOMED})
    engine = _new_engine(_db_url(tmp_path, "graphs.db"), shared_embedder)
    repo_id = _index(engine, repo)["repository_id"]
    (repo / "gone.py").unlink()
    _index(engine, repo)

    session = engine.storage.get_session()
    try:
        call_rows = [(e.source_file, e.target_file) for e in session.query(CallEdge).filter(CallEdge.repo_id == repo_id)]
        module_rows = [(e.source_file, e.target_file) for e in session.query(ModuleEdge).filter(ModuleEdge.repo_id == repo_id)]
    finally:
        session.close()
    assert not any("gone.py" in (src, tgt or "") for src, tgt in call_rows + module_rows)


def test_a_second_full_run_changes_nothing(tmp_path, shared_embedder):
    repo = _make_repo(tmp_path, "r9", {"keep.py": KEEP, "other.py": OTHER, "gone.py": DOOMED})
    engine = _new_engine(_db_url(tmp_path, "idempotent.db"), shared_embedder)
    repo_id = _index(engine, repo)["repository_id"]
    (repo / "gone.py").unlink()
    _index(engine, repo)
    settled = _db_state(engine, repo_id)

    _index(engine, repo)

    assert _db_state(engine, repo_id) == settled


def test_a_very_large_stale_set_does_not_overflow_the_sql_parameter_limit(tmp_path, shared_embedder):
    """`_purge_stale_entries` used to bind every path into one `IN (...)`; a big directory that
    becomes ignored can mean tens of thousands of stale paths."""
    repo = _make_repo(tmp_path, "r10", {"keep.py": KEEP, "other.py": OTHER, "gone.py": DOOMED})
    engine = _new_engine(_db_url(tmp_path, "many.db"), shared_embedder)
    repo_id = _index(engine, repo)["repository_id"]

    fake_paths = [f"never/existed/{i}.py" for i in range(40000)]
    engine._purge_stale_entries(repo_id, fake_paths + ["gone.py"])

    assert _db_state(engine, repo_id)["files"] == ["keep.py", "other.py"]
