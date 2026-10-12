"""
Phase 1.5 — Ingestion safety: path allow-list, SSRF check, size caps
(roadmap §37 item 1.5; §15 Security Architecture).

These tests exercise `codepulse.ingestion_safety.validate_local_path`/
`validate_git_url` directly -- no HTTP, no database -- against real
filesystem paths and real (but always literal-IP, never DNS-dependent)
host resolution, matching this project's established preference for
verifying real behavior over mocking it.

HTTP-level behavior (the actual `/v1/repositories/index` wiring, the 400/
501 responses, and the file-count/size caps enforced inside
`IndexingEngine.index_directory`) is covered separately in
tests/test_ingestion_safety_endpoint.py.
"""
from __future__ import annotations

import os

import pytest

from codepulse.ingestion_safety import IngestionSafetyError, validate_git_url, validate_local_path


# --- validate_local_path: allow-list ------------------------------------------

def test_unrestricted_allow_list_permits_any_real_path(tmp_path):
    """An empty allow-list (the default) means unrestricted -- preserving
    the local-first zero-configuration experience."""
    target = tmp_path / "repo"
    target.mkdir()
    resolved = validate_local_path(str(target), allowed_roots=[])
    assert resolved == str(target.resolve())


# --- validate_local_path: the path must be a real directory --------------------
# Found by the 2026-10-02 audit: a nonexistent path used to be accepted and
# silently created an empty "phantom" repository.

def test_nonexistent_path_is_rejected_even_when_unrestricted(tmp_path):
    with pytest.raises(IngestionSafetyError, match="not an existing directory"):
        validate_local_path(str(tmp_path / "does_not_exist"), allowed_roots=[])


def test_a_file_is_rejected_because_a_repository_must_be_a_directory(tmp_path):
    a_file = tmp_path / "file.py"
    a_file.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(IngestionSafetyError, match="not an existing directory"):
        validate_local_path(str(a_file), allowed_roots=[])


def test_nonexistent_path_inside_an_allowed_root_is_rejected(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    with pytest.raises(IngestionSafetyError, match="not an existing directory"):
        validate_local_path(str(root / "missing"), allowed_roots=[str(root)])


def test_nonexistent_path_outside_the_allow_list_reports_the_allow_list_not_existence(tmp_path):
    """The allow-list check must come first, so a caller can't use the error
    message to probe whether paths OUTSIDE the allowed roots exist."""
    root = tmp_path / "allowed"
    root.mkdir()
    with pytest.raises(IngestionSafetyError) as exc:
        validate_local_path(str(tmp_path / "elsewhere" / "missing"), allowed_roots=[str(root)])
    assert "outside" in str(exc.value)
    assert "existing directory" not in str(exc.value)


def test_path_inside_an_allowed_root_is_permitted(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    repo = root / "myrepo"
    repo.mkdir()

    resolved = validate_local_path(str(repo), allowed_roots=[str(root)])
    assert resolved == str(repo.resolve())


def test_path_equal_to_an_allowed_root_is_permitted(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    resolved = validate_local_path(str(root), allowed_roots=[str(root)])
    assert resolved == str(root.resolve())


def test_path_outside_every_allowed_root_is_rejected(tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(IngestionSafetyError):
        validate_local_path(str(outside), allowed_roots=[str(allowed_root)])


def test_path_with_a_sibling_name_sharing_a_prefix_is_not_falsely_permitted(tmp_path):
    """
    `/allowed` must not accidentally permit `/allowed-evil` via a naive
    string-prefix check -- the containment check requires an exact match
    or the allowed root followed by a real path separator.
    """
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    sibling = tmp_path / "allowed-evil"
    sibling.mkdir()

    with pytest.raises(IngestionSafetyError):
        validate_local_path(str(sibling), allowed_roots=[str(allowed_root)])


def test_dot_dot_traversal_escaping_the_allowed_root_is_rejected(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    (tmp_path / "secret").mkdir()

    traversal_path = str(root / ".." / "secret")
    with pytest.raises(IngestionSafetyError):
        validate_local_path(traversal_path, allowed_roots=[str(root)])


def test_dot_dot_traversal_staying_inside_the_allowed_root_is_permitted(tmp_path):
    root = tmp_path / "allowed"
    (root / "sub").mkdir(parents=True)
    target = root / "sub"

    traversal_but_still_inside = str(target / ".." / "sub")
    resolved = validate_local_path(traversal_but_still_inside, allowed_roots=[str(root)])
    assert resolved == str(target.resolve())


def test_symlink_escaping_the_allowed_root_is_rejected(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    link = root / "escape_link"

    try:
        os.symlink(str(outside), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(IngestionSafetyError):
        validate_local_path(str(link), allowed_roots=[str(root)])


# --- validate_git_url: scheme + SSRF host checks -------------------------------

def test_non_https_ssh_scheme_is_rejected():
    with pytest.raises(IngestionSafetyError):
        validate_git_url("ftp://example.com/repo.git")


def test_https_scheme_with_a_safe_public_ip_host_is_permitted():
    """Uses a literal public IP (Google's public DNS resolver) so this
    never depends on a real DNS lookup or network access."""
    validate_git_url("https://8.8.8.8/repo.git")  # must not raise


def test_ssh_scheme_with_a_safe_public_ip_host_is_permitted():
    validate_git_url("ssh://8.8.8.8/repo.git")  # must not raise


def test_loopback_host_is_rejected():
    with pytest.raises(IngestionSafetyError):
        validate_git_url("https://127.0.0.1/repo.git")


def test_link_local_metadata_host_is_rejected():
    """169.254.169.254 is the classic cloud-metadata SSRF target."""
    with pytest.raises(IngestionSafetyError):
        validate_git_url("https://169.254.169.254/repo.git")


def test_private_range_host_is_rejected():
    with pytest.raises(IngestionSafetyError):
        validate_git_url("https://10.0.0.5/repo.git")
    with pytest.raises(IngestionSafetyError):
        validate_git_url("https://192.168.1.1/repo.git")


def test_url_with_no_host_is_rejected():
    with pytest.raises(IngestionSafetyError):
        validate_git_url("https:///repo.git")
