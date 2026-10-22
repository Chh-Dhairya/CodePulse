"""
Every symbol has its own id (FUTURE_CHANGES item 31, 2026-10-18).

The defect: in-memory ids were `repo::file::name`, so same-named symbols in one
file (every class's `__init__`, `run`, `get`, property setters, overloads) shared
one id. The database kept every row, but memory kept one symbol, the lexical index
held several documents under one id, and the vector index had duplicate chunk
ids, so search, autocomplete, graph scores and the symbol counts could describe
or merge the wrong symbol. The roadmap (section 29, idempotency rule) already
defines a symbol's natural key as `(file_id, name, start_line)` and the database
enforces it; the in-memory ids simply dropped `start_line`.

Rules pinned down here:
  - the id is a pure function of (repository, file, name, start_line), matching
    the database's natural key, so it can be rebuilt from a database row alone;
  - after indexing, memory holds exactly one symbol per database row, one lexical
    document per symbol, and no duplicate vector ids;
  - search finds the right same-named symbol with the right lines, and a symbol's
    keyword and semantic evidence are combined (the earlier containment could not);
  - the same ids come back after a restart;
  - an edit that shifts lines replaces exactly the affected ids and leaves no
    stale documents behind.

A stand-in embedder (hashed bag of words) is used, so no model download is needed.
"""
from __future__ import annotations

import pytest

from codepulse.indexer.engine import IndexingEngine
from codepulse.storage.db import StorageManager, SymbolModel
from codepulse.symbol_identity import chunk_doc_id, symbol_doc_id
from tests.test_search_symbol_aggregation import _embedder

# --- the identity function ---------------------------------------------------------


def test_the_id_is_made_of_repository_file_name_and_start_line():
    assert symbol_doc_id(7, "pkg/mod.py", "__init__", 12) == "7::pkg/mod.py::__init__@12"


def test_a_chunk_id_extends_its_symbols_id():
    assert chunk_doc_id(7, "pkg/mod.py", "__init__", 12, 2) == "7::pkg/mod.py::__init__@12::2"
    assert chunk_doc_id(7, "pkg/mod.py", "__init__", 12, 2).startswith(symbol_doc_id(7, "pkg/mod.py", "__init__", 12))


def test_symbols_differing_in_any_one_part_get_different_ids():
    base = dict(repo_id=1, file_path="a.py", name="run", start_line=10)
    ids = {symbol_doc_id(**base)}
    for change in ({"repo_id": 2}, {"file_path": "b.py"}, {"name": "go"}, {"start_line": 11}):
        ids.add(symbol_doc_id(**{**base, **change}))
    assert len(ids) == 5


def test_chunks_of_different_symbols_never_share_an_id():
    assert chunk_doc_id(1, "a.py", "run", 10, 0) != chunk_doc_id(1, "a.py", "run", 20, 0)


# --- an engine over a file full of same-named symbols --------------------------------

_BODY = "".join(f"        step_{i} = compute(buffer, {i})\n" for i in range(60))
_SOURCE = f'''class Alpha:
    def __init__(self, a):
        """Alpha constructor stores the alpha value."""
        self.a = a

    def run(self):
        """Alpha run processes the alpha pipeline stages."""
{_BODY}        return self.a


class Beta:
    def __init__(self, b):
        """Beta constructor stores the beta value."""
        self.b = b

    def run(self):
        """Beta run processes the beta pipeline stages."""
{_BODY}        return self.b


class Gamma:
    def __init__(self, c):
        """Gamma constructor stores the gamma value."""
        self.c = c
'''


def _line_of(source: str, needle: str, occurrence: int = 1) -> int:
    seen = 0
    for number, line in enumerate(source.splitlines(), start=1):
        if needle in line:
            seen += 1
            if seen == occurrence:
                return number
    raise AssertionError(needle)


INIT_LINE = {
    "alpha": _line_of(_SOURCE, "def __init__", 1),
    "beta": _line_of(_SOURCE, "def __init__", 2),
    "gamma": _line_of(_SOURCE, "def __init__", 3),
}
RUN_LINE = {"alpha": _line_of(_SOURCE, "def run", 1), "beta": _line_of(_SOURCE, "def run", 2)}


@pytest.fixture
def indexed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(_SOURCE, encoding="utf-8")
    url = f"sqlite:///{(tmp_path / 'ident.db').as_posix()}"
    engine = IndexingEngine(storage=StorageManager(db_url=url), embedder=_embedder())
    repo_id = engine.index_directory(str(repo), repo_name="ident", repo_url=str(repo))["repository_id"]
    return engine, repo_id, repo, url


def _db_symbol_rows(engine):
    session = engine.storage.get_session()
    try:
        return [(r.name, r.start_line) for r in session.query(SymbolModel).all()]
    finally:
        session.close()


def _db_symbol_keys(engine):
    """(file path, name, start line) for every symbol row, across all files."""
    from codepulse.storage.db import FileModel

    session = engine.storage.get_session()
    try:
        return [(f.path, sm.name, sm.start_line) for sm, f in session.query(SymbolModel, FileModel).join(FileModel, SymbolModel.file_id == FileModel.id).all()]
    finally:
        session.close()


def test_memory_holds_exactly_one_symbol_per_database_row(indexed):
    engine, repo_id, _, _ = indexed
    rows = _db_symbol_rows(engine)
    assert len(rows) == 8  # 3 classes + 3 __init__ + 2 run (Gamma has no run)
    assert len(engine.symbols_by_id) == len(rows)
    assert sorted(engine.symbols_by_id) == sorted(symbol_doc_id(repo_id, "m.py", n, line) for n, line in rows)


def test_there_is_one_lexical_document_per_symbol_and_no_duplicate_vector_ids(indexed):
    engine, _, _, _ = indexed
    assert engine.bm25_index.num_docs == len(_db_symbol_rows(engine))
    vector_ids = engine.vector_index.doc_ids
    assert len(vector_ids) == len(set(vector_ids)), "duplicate chunk ids"


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
@pytest.mark.parametrize("who", ["alpha", "beta", "gamma"])
def test_search_returns_the_right_same_named_symbol_with_its_own_lines(indexed, mode, who):
    engine, repo_id, _, _ = indexed
    results = engine.search(f"{who} constructor stores the {who} value", limit=10, search_type=mode, repository_id=repo_id)
    first_init = next(r for r in results if r.symbol_name == "__init__")
    assert first_init.start_line == INIT_LINE[who], f"{mode}/{who}: got line {first_init.start_line}"
    assert first_init.doc_id == symbol_doc_id(repo_id, "m.py", "__init__", INIT_LINE[who])


@pytest.mark.parametrize("who", ["alpha", "beta"])
def test_a_same_named_symbols_keyword_and_semantic_evidence_are_combined(indexed, who):
    """The earlier containment kept these apart for shared ids; with unique ids hybrid fuses them."""
    engine, repo_id, _, _ = indexed
    results = engine.search(f"{who} run processes the {who} pipeline stages", limit=10, search_type="hybrid", repository_id=repo_id)
    top_run = next(r for r in results if r.symbol_name == "run")
    assert top_run.start_line == RUN_LINE[who]
    assert top_run.keyword_score > 0 and top_run.vector_score > 0


def test_multi_chunk_same_named_symbols_fold_to_one_row_each(indexed):
    engine, repo_id, _, _ = indexed
    chunks_per_run = {line: [d for d in engine.vector_index.doc_ids if d.startswith(f"{repo_id}::m.py::run@{line}::")] for line in RUN_LINE.values()}
    assert all(len(c) >= 2 for c in chunks_per_run.values()), "the fixture must give each run() several chunks"
    results = engine.search("run processes the pipeline stages", limit=20, search_type="semantic", repository_id=repo_id)
    run_rows = [r for r in results if r.symbol_name == "run"]
    assert sorted(r.start_line for r in run_rows) == sorted(RUN_LINE.values())


def test_the_ambiguity_workaround_is_gone(indexed):
    engine, _, _, _ = indexed
    assert not hasattr(engine, "ambiguous_symbol_ids")
    assert not hasattr(engine, "_disambiguate_lexical_hits")


# --- restart ------------------------------------------------------------------------


def test_the_same_ids_come_back_after_a_restart(indexed):
    engine, repo_id, _, url = indexed
    before = (sorted(engine.symbols_by_id), sorted(engine.vector_index.doc_ids), engine.bm25_index.num_docs)
    engine.storage.engine.dispose()

    restarted = IndexingEngine(storage=StorageManager(db_url=url), embedder=_embedder())

    assert sorted(restarted.symbols_by_id) == before[0]
    assert sorted(restarted.vector_index.doc_ids) == before[1]
    assert restarted.bm25_index.num_docs == before[2]
    results = restarted.search("beta constructor stores the beta value", limit=10, search_type="hybrid", repository_id=repo_id)
    assert next(r for r in results if r.symbol_name == "__init__").start_line == INIT_LINE["beta"]


# --- incremental updates -------------------------------------------------------------


FILLER_FILES = 3  # one symbol each; keeps the edited file under the 50% "full re-index is cheaper" threshold


def _write_fillers(repo):
    for i in range(FILLER_FILES):
        (repo / f"filler_{i}.py").write_text(f"def filler_{i}():\n    return {i}\n", encoding="utf-8")


def _git_repo(path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-q", "-m", "first")
    return git


def test_a_shift_in_lines_replaces_exactly_the_affected_ids_and_leaves_nothing_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(_SOURCE, encoding="utf-8")
    _write_fillers(repo)
    git = _git_repo(repo)
    url = f"sqlite:///{(tmp_path / 'shift.db').as_posix()}"
    engine = IndexingEngine(storage=StorageManager(db_url=url), embedder=_embedder())
    repo_id = engine.index_directory(str(repo), repo_name="shift", repo_url=str(repo))["repository_id"]

    # two lines added above Beta: Alpha's symbols are untouched, everything from Beta on moves down by 2
    shifted = _SOURCE.replace("class Beta:", "# note one\n# note two\nclass Beta:")
    (repo / "m.py").write_text(shifted, encoding="utf-8")
    git("commit", "-qam", "shift")
    result = engine.index_directory(str(repo), repo_name="shift", repo_url=str(repo))

    assert result["incremental"] is True
    keys = _db_symbol_keys(engine)
    expected_ids = sorted(symbol_doc_id(repo_id, path, n, line) for path, n, line in keys)
    assert sorted(engine.symbols_by_id) == expected_ids
    assert engine.bm25_index.num_docs == len(keys), "a stale lexical document survived the shift"
    vector_ids = engine.vector_index.doc_ids
    assert len(vector_ids) == len(set(vector_ids))
    assert not any(d.startswith(f"{repo_id}::m.py::__init__@{INIT_LINE['beta']}") for d in vector_ids), "Beta's old chunks are still indexed"
    results = engine.search("beta constructor stores the beta value", limit=10, search_type="hybrid", repository_id=repo_id)
    assert next(r for r in results if r.symbol_name == "__init__").start_line == INIT_LINE["beta"] + 2


def test_changing_one_same_named_symbols_body_leaves_its_siblings_untouched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(_SOURCE, encoding="utf-8")
    _write_fillers(repo)
    git = _git_repo(repo)
    engine = IndexingEngine(storage=StorageManager(db_url=f"sqlite:///{(tmp_path / 'one.db').as_posix()}"), embedder=_embedder())
    repo_id = engine.index_directory(str(repo), repo_name="one", repo_url=str(repo))["repository_id"]
    gamma_before = [d for d in engine.vector_index.doc_ids if d.startswith(f"{repo_id}::m.py::__init__@{INIT_LINE['gamma']}::")]

    edited = _SOURCE.replace("self.b = b", "self.b = b * 2  # changed")
    (repo / "m.py").write_text(edited, encoding="utf-8")
    git("commit", "-qam", "edit beta init")
    result = engine.index_directory(str(repo), repo_name="one", repo_url=str(repo))

    # 8 symbols in m.py; only Beta.__init__ and class Beta (whose body contains it) changed
    assert result["incremental"] is True
    assert result["symbols_skipped_unchanged"] == 6
    gamma_after = [d for d in engine.vector_index.doc_ids if d.startswith(f"{repo_id}::m.py::__init__@{INIT_LINE['gamma']}::")]
    assert gamma_after == gamma_before
    assert engine.bm25_index.num_docs == len(_db_symbol_rows(engine))


def test_deleting_the_file_removes_all_of_its_ids(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(_SOURCE, encoding="utf-8")
    (repo / "keep.py").write_text("def keeper():\n    return 1\n", encoding="utf-8")
    git = _git_repo(repo)
    engine = IndexingEngine(storage=StorageManager(db_url=f"sqlite:///{(tmp_path / 'del.db').as_posix()}"), embedder=_embedder())
    repo_id = engine.index_directory(str(repo), repo_name="del", repo_url=str(repo))["repository_id"]

    (repo / "m.py").unlink()
    git("add", "-A")
    git("commit", "-qm", "remove m.py")
    engine.index_directory(str(repo), repo_name="del", repo_url=str(repo))

    assert list(engine.symbols_by_id) == [symbol_doc_id(repo_id, "keep.py", "keeper", 1)]
    assert engine.bm25_index.num_docs == 1
    assert not any("m.py" in d for d in engine.vector_index.doc_ids)


# --- REST: autocomplete and the in-memory symbol count ----------------------------------


def test_autocomplete_lists_every_same_named_symbol_and_the_symbol_count_matches_the_database(isolated_app, tmp_path, monkeypatch):
    """Autocomplete walks the in-memory symbol map, so it listed ONE `__init__` for three classes, and
    `indexed_symbols` under-counted by the same amount."""
    from fastapi.testclient import TestClient

    import codepulse.api.app as app_module
    from tests.conftest import make_api_key_headers

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(_SOURCE, encoding="utf-8")
    engine = IndexingEngine(storage=isolated_app, embedder=_embedder())
    monkeypatch.setattr(app_module, "indexing_engine", engine)
    engine.index_directory(str(repo), repo_name="rest", repo_url=str(repo))
    client = TestClient(app_module.app)
    headers = make_api_key_headers(isolated_app)

    body = client.get("/v1/autocomplete", params={"prefix": "__init__", "limit": 10}, headers=headers).json()

    assert sorted(s["line"] for s in body["suggestions"]) == sorted(INIT_LINE.values())
    assert len(engine.symbols_by_id) == len(_db_symbol_keys(engine))
