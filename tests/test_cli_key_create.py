"""
`codepulse key create`: how a new user gets their first API key.

Found while preparing the VS Code extension for publication: the extension's key prompt and the README said "create one
with the CodePulse CLI", but the CLI had no key command, and `POST /v1/admin/keys` needs an admin key to already exist
(the backend README minted the first one with a Python one-liner). So nobody could start from a fresh install.

The command (`codepulse key create --repo <folder> [--role ...] [--name ...] [--json]`):
  - mints a key scoped to the given folders; a key with no repository grant can do nothing, so at least one is required;
  - registers a folder that is not in the database yet (an empty repository row, exactly what indexing would create),
    so "Index Workspace" in the editor can then index it with this key;
  - defaults to the `indexer` role: enough to search and to index, no key management (least privilege);
  - prints the raw key once, on stdout, and never stores it (only its hash is persisted);
  - checks the folders exist before it touches the database.

Temporary databases only; never the real codepulse.db.
"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import codepulse.cli as cli
from codepulse.auth import hash_api_key, register_repository
from codepulse.repository_identity import canonical_repository_url
from codepulse.storage.db import StorageManager


def _run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["codepulse", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def db(tmp_path):
    return f"sqlite:///{(tmp_path / 'keys.db').as_posix()}"


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "my project"
    folder.mkdir()
    return folder


def _rows(db_url, sql, *params):
    conn = sqlite3.connect(db_url[len("sqlite:///"):])
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --- creating a key -------------------------------------------------------------------------------------------------


def test_it_prints_the_raw_key_once_on_stdout_and_json_describes_it(db, project, monkeypatch, capsys):
    code, out, _ = _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--json")
    assert code == 0
    body = json.loads(out)
    assert re.fullmatch(r"cpk_[A-Za-z0-9_-]{40,}", body["key"])
    assert body["role"] == "indexer"
    assert body["name"]
    assert [r["path"] for r in body["repositories"]] == [canonical_repository_url(str(project))]
    assert body["repositories"][0]["registered"] is True


def test_the_raw_key_is_never_stored_only_its_hash(db, project, monkeypatch, capsys):
    _, out, _ = _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--json")
    raw = json.loads(out)["key"]
    stored = _rows(db, "SELECT key_hash, key_prefix FROM api_keys")
    assert stored == [(hash_api_key(raw), raw[:12])]
    dump = "".join(str(r) for t in ("api_keys", "api_key_repositories", "repositories") for r in _rows(db, f"SELECT * FROM {t}"))
    assert raw not in dump


def test_the_human_output_shows_the_key_and_what_to_do_next(db, project, monkeypatch, capsys):
    code, out, err = _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--name", "vscode")
    assert code == 0
    assert re.search(r"^cpk_\S+$", out, re.M), "the key must be on a line of its own, easy to copy"
    assert "vscode" in out
    assert "Set API Key" in out and "Index Workspace" in out
    assert "cpk_" not in err, "diagnostics go to stderr; the key must only ever be printed on stdout"


def test_the_default_role_is_indexer_and_the_role_can_be_chosen(db, project, monkeypatch, capsys):
    _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--json")
    _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--role", "admin", "--json")
    _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--role", "viewer", "--json")
    assert [r[0] for r in _rows(db, "SELECT role FROM api_key_repositories ORDER BY id")] == ["indexer", "admin", "viewer"]


def test_an_unknown_role_is_refused_and_nothing_is_created(db, project, monkeypatch, capsys):
    code, _, _ = _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--role", "root")
    assert code != 0
    assert not __import__("os").path.exists(db[len("sqlite:///"):])


def test_a_repo_is_required_because_a_key_without_a_grant_can_do_nothing(db, monkeypatch, capsys):
    code, _, err = _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create")
    assert code != 0
    assert "--repo" in err


# --- the folders ---------------------------------------------------------------------------------------------------


def test_a_folder_that_does_not_exist_is_refused_before_the_database_is_touched(db, tmp_path, monkeypatch, capsys):
    code, out, err = _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(tmp_path / "nope"), "--json")
    assert code == 1
    assert json.loads(out)["error"] == "path_not_found"
    assert not __import__("os").path.exists(db[len("sqlite:///"):]), "no database may be created for a mistyped path"


def test_an_unknown_folder_is_registered_as_an_empty_repository_and_a_known_one_is_reused(db, project, monkeypatch, capsys):
    _, first, _ = _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--json")
    _, second, _ = _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--json")
    a, b = json.loads(first)["repositories"][0], json.loads(second)["repositories"][0]
    assert a["registered"] is True and b["registered"] is False
    assert a["id"] == b["id"]
    assert _rows(db, "SELECT name, file_count, symbol_count FROM repositories") == [("my project", 0, 0)]


def test_several_folders_get_one_grant_each(db, project, tmp_path, monkeypatch, capsys):
    other = tmp_path / "other"
    other.mkdir()
    _, out, _ = _run_cli(monkeypatch, capsys, "--db-url", db, "key", "create", "--repo", str(project), "--repo", str(other), "--json")
    ids = [r["id"] for r in json.loads(out)["repositories"]]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert sorted(r[0] for r in _rows(db, "SELECT repo_id FROM api_key_repositories")) == sorted(ids)


def test_registering_a_repository_does_not_grant_anything_to_anyone(db, project):
    storage = StorageManager(db_url=db)
    repo_id, created = register_repository(storage, "p", str(project))
    assert created is True
    assert register_repository(storage, "p", str(project)) == (repo_id, False)
    assert _rows(db, "SELECT COUNT(*) FROM api_key_repositories") == [(0,)]


# --- the first-run flow, end to end ---------------------------------------------------------------------------------


def test_a_key_made_by_the_cli_works_against_the_server_and_can_index_its_folder(isolated_app, project, monkeypatch, capsys):
    """The real first run: a fresh database, the CLI mints the key, the editor extension uses it."""
    import codepulse.api.app as app_module
    from codepulse.indexer.engine import IndexingEngine
    from tests.test_index_persistence_integrity import _stub_embedder

    (project / "m.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    engine = IndexingEngine(storage=isolated_app, embedder=_stub_embedder())
    monkeypatch.setattr(app_module, "indexing_engine", engine)

    _, out, _ = _run_cli(monkeypatch, capsys, "--db-url", isolated_app.db_url, "key", "create", "--repo", str(project), "--json")
    body = json.loads(out)
    headers = {"X-API-Key": body["key"]}
    client = TestClient(app_module.app)
    repo_id = body["repositories"][0]["id"]

    assert client.get(f"/v1/repositories/{repo_id}/stats", headers=headers).status_code == 200
    started = client.post("/v1/repositories/index", json={"url": str(project)}, headers=headers)
    assert started.status_code in (200, 202), started.text
    assert started.json()["repository_id"] == repo_id, "indexing must reuse the registered repository, not create a second one"
    job = client.get(f"/v1/jobs/{started.json()['job_id']}", headers=headers).json()
    assert job["status"] == "succeeded", job
    assert len(_rows(isolated_app.db_url, "SELECT id FROM repositories")) == 1


def test_the_role_is_enforced_a_viewer_key_can_read_but_cannot_index(isolated_app, project, monkeypatch, capsys):
    import codepulse.api.app as app_module

    _, out, _ = _run_cli(monkeypatch, capsys, "--db-url", isolated_app.db_url, "key", "create", "--repo", str(project), "--role", "viewer", "--json")
    body = json.loads(out)
    headers = {"X-API-Key": body["key"]}
    client = TestClient(app_module.app)

    assert client.get(f"/v1/repositories/{body['repositories'][0]['id']}/stats", headers=headers).status_code == 200
    assert client.post("/v1/repositories/index", json={"url": str(project)}, headers=headers).status_code == 403


def test_a_key_for_one_folder_cannot_read_another(isolated_app, project, tmp_path, monkeypatch, capsys):
    import codepulse.api.app as app_module

    other = tmp_path / "someone-elses-project"
    other.mkdir()
    _, mine, _ = _run_cli(monkeypatch, capsys, "--db-url", isolated_app.db_url, "key", "create", "--repo", str(project), "--json")
    _, theirs, _ = _run_cli(monkeypatch, capsys, "--db-url", isolated_app.db_url, "key", "create", "--repo", str(other), "--json")
    other_id = json.loads(theirs)["repositories"][0]["id"]

    assert TestClient(app_module.app).get(f"/v1/repositories/{other_id}/stats", headers={"X-API-Key": json.loads(mine)["key"]}).status_code == 403
