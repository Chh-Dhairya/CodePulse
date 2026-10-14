"""
One repository, one identity, however its path is spelled
(CODEPULSE_FUTURE_CHANGES.md item 2; user-approved 2026-10-14).

A repository was identified by the EXACT string it was first indexed under, so
the same directory spelled with backslashes vs forward slashes, a trailing
separator, `..` segments, or (found by the real-VS-Code tests) a lowercase vs
uppercase Windows drive letter (`c:\\x` vs `C:\\x`) became two repositories:
re-indexing created a duplicate with a new id, and, because the differently
spelled path looked brand-new to the "re-indexing needs `indexer`" check, a
viewer-only key could slip past it.

Rule: a local-path repository URL is stored and compared in ONE canonical form
(`codepulse.repository_identity.canonical_repository_url`: absolute, symlinks
and `..` resolved, true case and uppercase drive on Windows, no trailing
separator). Anything that is not a local path (a git URL, `file:///...`) is left
exactly as given. Lookups also find rows stored under another spelling (a
database not yet migrated) instead of creating a duplicate; a data migration
canonicalizes existing rows and never merges or deletes duplicates.

Temporary databases only.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import codepulse.cli as cli
from codepulse.api.app import app
from codepulse.auth import create_repository_owned_by
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.indexer.engine import IndexingEngine
from codepulse.repository_identity import canonical_repository_url, find_repository
from codepulse.storage.db import RepositoryModel, StorageManager
from tests.conftest import make_api_key_headers


@pytest.fixture(scope="module")
def shared_embedder():
    return CodeEmbedder()


def _spellings(path) -> list[str]:
    """Every way a user or tool might write this existing directory."""
    p = str(path)
    spellings = [p, p + os.sep, p.replace("\\", "/"), p.replace("\\", "/") + "/", os.path.join(p, "sub", "..")]
    if os.name == "nt":
        spellings += [p[0].lower() + p[1:], p.lower(), p.upper().replace("\\", "/")]
    return spellings


def _repo_dir(tmp_path, name="proj"):
    path = tmp_path / name
    (path / "sub").mkdir(parents=True)
    (path / "mod.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    return path


def _repo_count(storage):
    session = storage.get_session()
    try:
        return session.query(RepositoryModel).count()
    finally:
        session.close()


# --- canonical_repository_url ----------------------------------------------

def test_every_spelling_of_one_directory_has_one_canonical_form(tmp_path):
    path = _repo_dir(tmp_path)
    forms = {canonical_repository_url(s) for s in _spellings(path)}
    assert len(forms) == 1, forms


def test_the_canonical_form_is_absolute_without_a_trailing_separator(tmp_path):
    path = _repo_dir(tmp_path)
    canonical = canonical_repository_url(str(path) + os.sep)
    assert os.path.isabs(canonical)
    assert not canonical.endswith(("/", "\\"))


def test_a_relative_path_is_made_absolute(tmp_path, monkeypatch):
    path = _repo_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert canonical_repository_url("proj") == canonical_repository_url(str(path))


@pytest.mark.skipif(os.name != "nt", reason="drive letters and case-insensitive paths are a Windows matter")
def test_windows_drive_letter_and_case_are_normalized(tmp_path):
    path = _repo_dir(tmp_path)
    canonical = canonical_repository_url(str(path))
    assert canonical[0].isupper() and canonical[1] == ":"
    assert canonical_repository_url(str(path).lower()) == canonical
    assert canonical_repository_url(str(path)[0].lower() + str(path)[1:]) == canonical


def test_a_symlink_resolves_to_its_target(tmp_path):
    target = _repo_dir(tmp_path)
    link = tmp_path / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this environment cannot create symlinks")
    assert canonical_repository_url(str(link)) == canonical_repository_url(str(target))


@pytest.mark.parametrize(
    "url",
    ["https://github.com/org/repo", "https://github.com/org/repo.git", "ssh://git@host/org/repo", "file:///r1", "file:///seed/anchor"],
)
def test_anything_that_is_not_a_local_path_is_left_exactly_as_given(url):
    assert canonical_repository_url(url) == url


def test_a_path_that_does_not_exist_is_still_made_canonical_without_error(tmp_path):
    missing = tmp_path / "gone" / "deeper"
    canonical = canonical_repository_url(str(missing) + os.sep)
    assert canonical == canonical_repository_url(str(missing))
    assert not canonical.endswith(("/", "\\"))


# --- the engine --------------------------------------------------------------

def test_indexing_the_same_directory_under_different_spellings_yields_one_repository(tmp_path, shared_embedder):
    path = _repo_dir(tmp_path)
    engine = IndexingEngine(storage=StorageManager(db_url=f"sqlite:///{(tmp_path / 'i.db').as_posix()}"), embedder=shared_embedder)
    ids = set()
    for spelling in _spellings(path):
        result = engine.index_directory(spelling, repo_name="proj", repo_url=spelling)
        ids.add(result["repository_id"])

    assert len(ids) == 1
    assert _repo_count(engine.storage) == 1


def test_the_stored_url_is_the_canonical_form(tmp_path, shared_embedder):
    path = _repo_dir(tmp_path)
    engine = IndexingEngine(storage=StorageManager(db_url=f"sqlite:///{(tmp_path / 's.db').as_posix()}"), embedder=shared_embedder)
    engine.index_directory(str(path) + os.sep, repo_name="proj")

    session = engine.storage.get_session()
    try:
        stored = session.query(RepositoryModel).one().url
    finally:
        session.close()
    assert stored == canonical_repository_url(str(path))


def test_hydration_finds_the_repository_under_any_spelling(tmp_path, shared_embedder):
    path = _repo_dir(tmp_path)
    db_url = f"sqlite:///{(tmp_path / 'h.db').as_posix()}"
    engine = IndexingEngine(storage=StorageManager(db_url=db_url), embedder=shared_embedder)
    repo_id = engine.index_directory(str(path), repo_name="proj")["repository_id"]

    result = engine.hydrate_repository(_spellings(path)[1])

    assert result["repo_id"] == repo_id


# --- find_repository ---------------------------------------------------------

def test_find_repository_matches_a_row_stored_under_another_spelling(tmp_path):
    """A database that has not been migrated: the row keeps its old spelling."""
    path = _repo_dir(tmp_path)
    storage = StorageManager(db_url=f"sqlite:///{(tmp_path / 'f.db').as_posix()}")
    legacy_spelling = str(path) + os.sep
    session = storage.get_session()
    try:
        row = RepositoryModel(name="proj", url=legacy_spelling)
        session.add(row)
        session.commit()
        row_id = row.id
        assert find_repository(session, str(path)).id == row_id
        assert find_repository(session, str(path).replace("\\", "/")).id == row_id
        assert find_repository(session, str(tmp_path / "nothing")) is None
    finally:
        session.close()


def test_find_repository_prefers_the_exact_canonical_row_when_duplicates_exist(tmp_path):
    path = _repo_dir(tmp_path)
    storage = StorageManager(db_url=f"sqlite:///{(tmp_path / 'd.db').as_posix()}")
    session = storage.get_session()
    try:
        legacy = RepositoryModel(name="legacy", url=str(path) + os.sep)
        session.add(legacy)
        session.commit()
        canonical = RepositoryModel(name="canonical", url=canonical_repository_url(str(path)))
        session.add(canonical)
        session.commit()
        assert find_repository(session, str(path)).id == canonical.id
    finally:
        session.close()


# --- the REST endpoint -------------------------------------------------------

def test_reindexing_with_a_different_spelling_is_the_same_repository_and_keeps_the_role_check(isolated_app):
    """The original bypass: a differently spelled path looked brand-new, so a viewer-only
    key was not subject to the 'existing repository needs indexer' check."""
    import tempfile

    client = TestClient(app)
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        path = _repo_dir(Path(tmp))
        # an anchor so a key may create a repository, then create the real one with the exact spelling
        session = isolated_app.get_session()
        try:
            anchor = RepositoryModel(name="anchor", url="file:///anchor/identity")
            session.add(anchor)
            session.commit()
            anchor_id = anchor.id
        finally:
            session.close()
        creator = make_api_key_headers(isolated_app, repository_ids=[anchor_id], role="indexer", name="creator")
        first = client.post("/v1/repositories/index", json={"url": str(path)}, headers=creator)
        assert first.status_code == 202, first.text
        repo_id = first.json()["repository_id"]
        viewer = make_api_key_headers(isolated_app, repository_ids=[repo_id], role="viewer", name="viewer")
        count_before = _repo_count(isolated_app)

        for spelling in _spellings(path)[1:]:
            as_viewer = client.post("/v1/repositories/index", json={"url": spelling}, headers=viewer)
            assert as_viewer.status_code == 403, f"viewer must not re-index via {spelling!r}: {as_viewer.text}"
            as_creator = client.post("/v1/repositories/index", json={"url": spelling}, headers=creator)
            assert as_creator.status_code == 202, f"{spelling!r}: {as_creator.text}"
            assert as_creator.json()["repository_id"] == repo_id
            assert as_creator.json()["url"] == canonical_repository_url(str(path))
            # wait out the (synchronously run) job so the next call is not a 409
        assert _repo_count(isolated_app) == count_before, "no duplicate repository may be created"


def test_owner_creation_recognizes_another_spelling_of_an_existing_repository(isolated_app, tmp_path):
    path = _repo_dir(tmp_path)
    session = isolated_app.get_session()
    try:
        a = RepositoryModel(name="a", url="file:///a")
        session.add(a)
        session.commit()
        a_id = a.id
    finally:
        session.close()
    first = make_api_key_headers(isolated_app, repository_ids=[a_id], role="indexer", name="first")
    second = make_api_key_headers(isolated_app, repository_ids=[a_id], role="indexer", name="second")
    from codepulse.auth import verify_api_key

    first_id = verify_api_key(isolated_app, first["X-API-Key"])
    second_id = verify_api_key(isolated_app, second["X-API-Key"])

    repo_id, created = create_repository_owned_by(isolated_app, "proj", str(path), first_id)
    same_id, created_again = create_repository_owned_by(isolated_app, "proj", str(path) + os.sep, second_id)

    assert created is True and created_again is False
    assert same_id == repo_id


# --- the CLI -----------------------------------------------------------------

def test_cli_status_finds_the_repository_under_any_spelling(tmp_path, shared_embedder, monkeypatch, capsys):
    path = _repo_dir(tmp_path)
    db_url = f"sqlite:///{(tmp_path / 'c.db').as_posix()}"
    engine = IndexingEngine(storage=StorageManager(db_url=db_url), embedder=shared_embedder)
    repo_id = engine.index_directory(str(path), repo_name="proj")["repository_id"]
    engine.storage.engine.dispose()

    for spelling in _spellings(path):
        monkeypatch.setattr("sys.argv", ["codepulse", "--db-url", db_url, "status", spelling, "--json"])
        cli.main()
        out = capsys.readouterr().out
        assert '"indexed": true' in out and f'"repository_id": {repo_id}' in out, spelling
