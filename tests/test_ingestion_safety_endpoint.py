"""
Phase 1.5 — Ingestion safety, HTTP layer (roadmap §37 item 1.5; backlog
B10 DoD: "Path traversal / SSRF / oversized-repo attempts rejected with
tests proving it").

Exercises the real `/v1/repositories/index` endpoint end-to-end:
  - a git-URL (`source != "local"`) request with an SSRF-unsafe host is
    rejected (400) before anything else happens
  - a git-URL request with a bad scheme is rejected (400)
  - a git-URL request that is otherwise *safe* still gets an honest 501
    (no git-clone implementation exists -- see
    codepulse/ingestion_safety.py's module docstring)
  - a local path outside a configured allow-list is rejected (400); one
    inside it succeeds; the default (unconfigured) allow-list remains
    unrestricted
  - a repository whose file count exceeds the configured cap fails its
    job with a real, non-fabricated error message (checked via
    GET /v1/jobs/{job_id}, since this can only be discovered once
    IndexingEngine has walked the directory -- see
    codepulse/indexer/engine.py's comment on why this isn't a
    synchronous rejection like the checks above)
  - an oversized file is skipped, not fatal to the run -- the job still
    succeeds, with the real (reduced) file count in its result

All tests use the shared `isolated_app` fixture (tests/conftest.py): a
temporary, disposable SQLite database, never the real codepulse.db.
codepulse.ingestion_safety's own validation logic is tested directly (not
via HTTP) in tests/test_ingestion_safety.py.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from codepulse.api.app import app
from codepulse.config import CodePulseConfig, reset_settings, set_settings
from tests.conftest import make_api_key_headers


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup_settings():
    reset_settings()
    yield
    reset_settings()


def _make_repo_dir(tmp_path, name: str, files: dict[str, str] | None = None):
    repo_dir = tmp_path / name
    repo_dir.mkdir()
    for rel_path, content in (files or {}).items():
        (repo_dir / rel_path).write_text(content, encoding="utf-8")
    return repo_dir


# --- Git-URL ingestion: SSRF check + honest 501 ------------------------------

def test_git_source_with_ssrf_unsafe_host_is_rejected(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    response = client.post(
        "/v1/repositories/index",
        json={"source": "github", "url": "https://169.254.169.254/org/repo"},
        headers=headers,
    )
    assert response.status_code == 400
    # Phase 1.6: the structured {"error": {"code", "message", "details"}}
    # envelope replaces the old plain {"detail": "..."} shape.
    message = response.json()["error"]["message"].lower()
    assert "ssrf" in message or "private" in message


def test_git_source_with_bad_scheme_is_rejected(client, isolated_app):
    headers = make_api_key_headers(isolated_app)
    response = client.post(
        "/v1/repositories/index",
        json={"source": "github", "url": "ftp://example.com/org/repo"},
        headers=headers,
    )
    assert response.status_code == 400


def test_git_source_with_a_safe_url_returns_honest_not_implemented(client, isolated_app):
    """
    A syntactically/SSRF-safe git URL still gets a clear 501 -- no
    git-clone implementation exists in this codebase (see
    codepulse/ingestion_safety.py's module docstring) -- rather than being
    silently mis-processed as a phantom local path.
    """
    headers = make_api_key_headers(isolated_app)
    response = client.post(
        "/v1/repositories/index",
        json={"source": "github", "url": "https://8.8.8.8/org/repo"},
        headers=headers,
    )
    assert response.status_code == 501


# --- Local-path ingestion: allow-list -----------------------------------------

def test_local_path_outside_configured_allow_list_is_rejected(client, isolated_app, tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_repo = _make_repo_dir(tmp_path, "outside_repo")

    set_settings(CodePulseConfig(ingestion_allowed_roots=[str(allowed_root)]))
    headers = make_api_key_headers(isolated_app)

    response = client.post(
        "/v1/repositories/index", json={"url": str(outside_repo)}, headers=headers
    )
    assert response.status_code == 400


def test_local_path_inside_configured_allow_list_succeeds(client, isolated_app, tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    inside_repo = allowed_root / "myrepo"
    inside_repo.mkdir()

    set_settings(CodePulseConfig(ingestion_allowed_roots=[str(allowed_root)]))
    headers = make_api_key_headers(isolated_app)

    response = client.post(
        "/v1/repositories/index", json={"url": str(inside_repo)}, headers=headers
    )
    assert response.status_code == 202


def test_local_path_unrestricted_by_default(client, isolated_app, tmp_path):
    """No ingestion_allowed_roots configured -- any real path is permitted,
    preserving the local-first zero-configuration experience."""
    repo_dir = _make_repo_dir(tmp_path, "anywhere")
    headers = make_api_key_headers(isolated_app)

    response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    assert response.status_code == 202


# --- File-count cap: fails the job (discoverable only after the walk) -------

def test_file_count_exceeding_cap_fails_the_job(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(
        tmp_path, "big_repo", {"a.py": "x = 1\n", "b.py": "x = 2\n", "c.py": "x = 3\n"}
    )
    set_settings(CodePulseConfig(ingestion_max_files_per_repo=2))
    headers = make_api_key_headers(isolated_app)

    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    assert post_response.status_code == 202
    job_id = post_response.json()["job_id"]
    repo_id = post_response.json()["repository_id"]

    scoped_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    get_response = client.get(f"/v1/jobs/{job_id}", headers=scoped_headers)
    assert get_response.status_code == 200
    data = get_response.json()
    assert data["status"] == "failed"
    assert "ingestion_max_files_per_repo" in data["error"]
    assert data["result"] is None


def test_file_count_within_cap_succeeds(client, isolated_app, tmp_path):
    repo_dir = _make_repo_dir(tmp_path, "small_repo", {"a.py": "x = 1\n"})
    set_settings(CodePulseConfig(ingestion_max_files_per_repo=10))
    headers = make_api_key_headers(isolated_app)

    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    job_id = post_response.json()["job_id"]
    repo_id = post_response.json()["repository_id"]

    scoped_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    get_response = client.get(f"/v1/jobs/{job_id}", headers=scoped_headers)
    assert get_response.json()["status"] == "succeeded"


def test_file_count_exactly_at_the_cap_succeeds(client, isolated_app, tmp_path):
    """
    Boundary case: a repository with exactly `ingestion_max_files_per_repo`
    files must succeed -- the configured value is the maximum ALLOWED
    count (inclusive), not an exclusive ceiling. Mirrors the `> max_files`
    (not `>=`) comparison in IndexingEngine.index_directory.
    """
    repo_dir = _make_repo_dir(
        tmp_path, "exact_repo", {"a.py": "x = 1\n", "b.py": "x = 2\n"}
    )
    set_settings(CodePulseConfig(ingestion_max_files_per_repo=2))  # exactly 2 files exist
    headers = make_api_key_headers(isolated_app)

    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    job_id = post_response.json()["job_id"]
    repo_id = post_response.json()["repository_id"]

    scoped_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    get_response = client.get(f"/v1/jobs/{job_id}", headers=scoped_headers)
    assert get_response.json()["status"] == "succeeded"


# --- Per-file size cap: skipped, not fatal ------------------------------------

def test_oversized_file_is_skipped_but_the_job_still_succeeds(client, isolated_app, tmp_path):
    small_content = "x = 1\n"  # well under any reasonable byte cap
    large_content = "y = 2\n" * 1000  # 6000 bytes, exceeds the tiny cap below

    repo_dir = _make_repo_dir(
        tmp_path, "mixed_repo", {"small.py": small_content, "large.py": large_content}
    )
    set_settings(CodePulseConfig(ingestion_max_file_size_bytes=len(small_content) + 10))
    headers = make_api_key_headers(isolated_app)

    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    job_id = post_response.json()["job_id"]
    repo_id = post_response.json()["repository_id"]

    scoped_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    get_response = client.get(f"/v1/jobs/{job_id}", headers=scoped_headers)
    data = get_response.json()

    assert data["status"] == "succeeded"
    # Only the small file was actually parsed -- the oversized one was
    # skipped (logged, not fatal), matching the existing per-file
    # error-tolerance pattern for malformed files.
    assert data["result"]["total_files"] == 1


def test_file_exactly_at_the_size_cap_is_not_skipped(client, isolated_app, tmp_path):
    """
    Boundary case: a file whose size is exactly `ingestion_max_file_size_bytes`
    must still be parsed -- the configured value is the maximum ALLOWED
    size (inclusive), not an exclusive ceiling. Mirrors the `> max_file_size`
    (not `>=`) comparison in IndexingEngine.index_directory. The cap is set
    from the file's REAL on-disk size (`os.path.getsize`), not `len(content)`
    -- Windows' text-mode write translates `\\n` to `\\r\\n`, so those two
    would otherwise differ.
    """
    exact_content = "x = 1\n"
    repo_dir = _make_repo_dir(tmp_path, "boundary_repo", {"a.py": exact_content})
    real_size_on_disk = os.path.getsize(repo_dir / "a.py")

    set_settings(CodePulseConfig(ingestion_max_file_size_bytes=real_size_on_disk))
    headers = make_api_key_headers(isolated_app)

    post_response = client.post("/v1/repositories/index", json={"url": str(repo_dir)}, headers=headers)
    job_id = post_response.json()["job_id"]
    repo_id = post_response.json()["repository_id"]

    scoped_headers = make_api_key_headers(isolated_app, repository_ids=[repo_id])
    get_response = client.get(f"/v1/jobs/{job_id}", headers=scoped_headers)
    data = get_response.json()

    assert data["status"] == "succeeded"
    assert data["result"]["total_files"] == 1, "a file exactly at the size cap must be parsed, not skipped"
