"""
Search quality on codebases that are NOT CodePulse (FUTURE_CHANGES item 20, part 2).

The main relevance gate (tests/test_relevance_eval.py) queries CodePulse's own
source, which is also what the ranking was originally tuned on, and 100 of its
141 queries are the symbol's own docstring (a keyword search scores 1.0 on
those). That says little about how search behaves on someone else's code, and
it let "hybrid" lose to semantic search alone on realistic queries unnoticed.

This file indexes two installed third-party packages with the REAL embedding
model and gates hybrid search on paraphrase queries written by hand from
reading each symbol's code (not from its docstring):

  - `starlette` (a web framework): tests/relevance/external_starlette.json
  - `pathspec`  (gitignore-style matching): tests/relevance/external_pathspec.json

`pathspec` was held out while the scaling change was chosen (starlette and
CodePulse's own paraphrases were the tuning data), so it is the honest
generalisation check.

Besides absolute floors, it pins the property that was wrong: pooled over all
paraphrase queries, hybrid ranks the right symbol higher (MRR) than semantic
search alone and than keyword search alone.

Both packages are dependencies of CodePulse, pinned to ranges; if a release
inside the range renames a symbol the dataset references, the structural test
fails loudly rather than the gate quietly scoring a broken dataset.

Real model + real indexing, so this file takes a couple of minutes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codepulse.config import reset_settings
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.parser.factory import get_parser_for_file
from codepulse.storage.db import StorageManager
from tests.relevance.eval_harness import evaluate, load_relevance_dataset

RELEVANCE_DIR = Path(__file__).parent / "relevance"
CODEBASES = {
    "starlette": RELEVANCE_DIR / "external_starlette.json",
    "pathspec": RELEVANCE_DIR / "external_pathspec.json",
}
SIZE_BAND = (30, 100)


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


def _package_dir(name: str) -> Path:
    module = __import__(name)
    return Path(module.__file__).parent


def _pairs(name: str):
    return load_relevance_dataset(CODEBASES[name], size_band=SIZE_BAND)


@pytest.fixture(scope="module")
def shared_embedder():
    return CodeEmbedder()


@pytest.fixture(scope="module")
def engines(tmp_path_factory, shared_embedder):
    out = {}
    for name in CODEBASES:
        package = _package_dir(name)
        db = tmp_path_factory.mktemp(f"external_{name}") / "eval.db"
        storage = StorageManager(db_url=f"sqlite:///{str(db).replace(os.sep, '/')}")
        engine = IndexingEngine(storage=storage, embedder=shared_embedder)
        result = engine.index_directory(str(package), repo_name=name, repo_url=str(package))
        out[name] = (engine, result["repository_id"])
    return out


def _evaluate(engines, name, mode):
    engine, repo_id = engines[name]
    return evaluate(
        lambda q, limit: engine.search(q, limit=limit, search_type=mode, repository_id=repo_id),
        pairs=_pairs(name),
        k=10,
        search_limit=20,
    )


# --- the datasets themselves -----------------------------------------------------

@pytest.mark.parametrize("name", list(CODEBASES))
def test_every_expected_symbol_exists_in_the_installed_package(name):
    package = _package_dir(name)
    real = set()
    for path in package.rglob("*.py"):
        parser = get_parser_for_file(str(path))
        if parser is None:
            continue
        rel = path.relative_to(package).as_posix()
        for sym in parser.extract_symbols(path.read_text(encoding="utf-8", errors="replace"), rel):
            real.add((rel, sym.name))
    missing = [(p.expected_file_path, p.expected_symbol_name) for p in _pairs(name) if (p.expected_file_path, p.expected_symbol_name) not in real]
    assert missing == [], f"{name}: the dataset references symbols the installed version no longer has: {missing}"


@pytest.mark.parametrize("name", list(CODEBASES))
def test_the_external_datasets_have_no_duplicate_queries(name):
    queries = [p.query for p in _pairs(name)]
    assert len(queries) == len(set(queries))


# --- the gate ---------------------------------------------------------------------

# Floors sit a real margin under the measured values (2026-10-18, real model):
#   starlette hybrid Recall@10 0.837 / MRR 0.529;  pathspec hybrid 0.903 / 0.719.
FLOORS = {"starlette": (0.78, 0.46), "pathspec": (0.85, 0.62)}


@pytest.mark.parametrize("name", list(CODEBASES))
def test_hybrid_search_meets_the_floor_on_an_external_codebase(engines, name):
    recall_floor, mrr_floor = FLOORS[name]
    result = _evaluate(engines, name, "hybrid")
    misses = "\n".join(
        f"  - {m.pair.query!r} -> expected {m.pair.expected_file_path}::{m.pair.expected_symbol_name}" for m in result.misses()[:12]
    )
    assert result.recall_at_10 >= recall_floor, f"{name}: Recall@10 {result.recall_at_10:.3f} < {recall_floor}\n{misses}"
    assert result.mrr >= mrr_floor, f"{name}: MRR {result.mrr:.3f} < {mrr_floor}"


def test_hybrid_ranks_the_right_symbol_above_either_of_its_parts(engines):
    """The property that was broken: on realistic queries 'hybrid' must not lose to semantic or keyword alone.
    Pooled over both codebases' paraphrases (74 queries)."""
    pooled = {}
    for mode in ("hybrid", "semantic", "keyword"):
        total_rr = total_hits = total_n = 0
        for name in CODEBASES:
            r = _evaluate(engines, name, mode)
            total_rr += r.mrr * r.num_queries
            total_hits += r.recall_at_10 * r.num_queries
            total_n += r.num_queries
        pooled[mode] = (total_hits / total_n, total_rr / total_n)
    detail = ", ".join(f"{m}: R@10 {v[0]:.3f} MRR {v[1]:.3f}" for m, v in pooled.items())
    assert pooled["hybrid"][1] > pooled["semantic"][1], f"hybrid MRR must beat semantic-only: {detail}"
    assert pooled["hybrid"][1] > pooled["keyword"][1], f"hybrid MRR must beat keyword-only: {detail}"
    assert pooled["hybrid"][0] >= pooled["keyword"][0], f"hybrid recall must not trail keyword-only: {detail}"
