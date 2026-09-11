"""
Phase 1.5 — Ingestion safety: path allow-list, SSRF check, size caps
(roadmap §37 item 1.5; §15 Security Architecture's "Repository-ingestion
safety (the dangerous endpoint)" paragraph; backlog B10, depends on B8).

Roadmap text this milestone implements, verbatim (§15):
  "for local-path ingestion, restrict to an explicit allow-listed root
  directory (configurable), reject `..`-traversal and symlink escapes;
  for Git-URL ingestion, validate the URL scheme (`https`/`ssh` only),
  resolve and check the target host isn't a private/link-local IP (SSRF
  prevention), and enforce a repository size/file-count cap before
  beginning a full clone."
Plus §15's "Resource limits: max files per repo, max file size parsed
[...]".

Scope, precisely (do not infer requirements from later milestones):
  - `validate_local_path`: resolves a requested local path (following
    symlinks, collapsing `..`) and, if one or more allow-listed roots are
    configured, rejects it unless the resolved path is contained in one
    of them. Catches both textual `..`-traversal and symlink-escape
    attempts the same way, because both are neutralized by resolution
    before the containment check ever runs.
  - `validate_git_url`: validates a git URL's scheme is `https`/`ssh`,
    resolves its host, and rejects it if any resolved address is
    private/loopback/link-local/reserved/multicast (SSRF prevention).
    This is a real, independently testable safety check -- see the note
    below on why it exists even though this codebase has no git-clone
    implementation yet.
  - File-count and per-file-size caps are enforced inside
    `IndexingEngine.index_directory()` itself (not here -- see that
    method's own comments), because the file list they check against is
    only available after the discovery walk `index_directory()` already
    performs; duplicating that walk here just to pre-count would be
    wasteful and is exactly the kind of "second identity system"/
    duplicated-logic this project's established house style avoids.
    `IngestionSafetyError` (this module) is the one exception type both
    that engine-level check and this module's own boundary checks raise,
    so `codepulse/api/app.py` has a single, consistent type to catch.

IMPORTANT scope note on `validate_git_url`: this codebase has **no git
clone/fetch implementation at all** (grep-confirmed: no `subprocess`,
`GitPython`, or network-clone code exists anywhere). `IndexRepoRequest
.source` (`"local"`/`"github"`/`"gitlab"`) has always been accepted but
never actually branched on -- every prior phase's `/v1/repositories/index`
treated `req.url` as a local filesystem path regardless of `source`,
silently indexing zero files for a real git URL. Implementing actual
git-URL cloning is a much larger, unrequested feature no sprint item in
this roadmap assigns a milestone to; it is NOT this milestone's job to
invent it. What Phase 1.5 *does* own, literally, is the "SSRF check" --
so this module implements and tests that check as a real, standalone
safety primitive (ready for whenever a real fetch path exists), and
`codepulse/api/app.py` now honestly rejects a non-local `source` (after
still validating the URL for safety first) with HTTP 501, rather than
continuing to silently mis-process it as a phantom local path.

Deliberately NOT in scope for Phase 1.5 (do not infer these belong here):
  - Actually cloning/fetching a git repository (see above).
  - Request-body size caps (roadmap §15 lists this as a separate
    "Resource limit" alongside file/repo caps; FastAPI/Starlette/uvicorn
    request-body size limits are a server/ASGI-layer configuration
    concern, not application code this milestone adds).
  - Max concurrent indexing jobs "per tenant" (roadmap §15) -- Phase 1.3
    already added a *per-repository* advisory one-active-job lock; a
    global/per-tenant concurrent-job cap is a distinct, unrequested
    control this milestone does not add.
  - A structured `{"error": {...}}` response envelope -- Phase 1.6.
  - A dedicated security test suite -- Phase 1.7.

This module lives outside `codepulse/indexer/` and `codepulse/api/`
deliberately -- a top-level module, mirroring `codepulse/auth.py`,
`codepulse/jobs.py`, and `codepulse/rate_limit.py`'s placement, per the
roadmap's "control plane"/API-boundary grouping of these safety concerns,
reusable identically by a future CLI or MCP surface.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_GIT_URL_SCHEMES = ("https", "ssh")


class IngestionSafetyError(Exception):
    """
    Raised for any ingestion request rejected on safety grounds: a local
    path outside the configured allow-list (or escaping it via `..`/a
    symlink), an unsafe git URL scheme, an SSRF-unsafe resolved host, or
    (raised by `IndexingEngine.index_directory` itself, not this module)
    a repository exceeding the configured file-count cap.
    """


def validate_local_path(path: str, allowed_roots: list[str]) -> str:
    """
    Resolves `path` (following symlinks, collapsing `..`) and, if
    `allowed_roots` is non-empty, rejects it unless the resolved path is
    contained in (or equal to) one of them. Returns the resolved,
    canonical path on success.

    An empty `allowed_roots` means "unrestricted" -- see
    `CodePulseConfig.ingestion_allowed_roots`'s docstring for why that is
    this project's deliberate default, preserving the local-first
    zero-configuration experience.
    """
    resolved = str(Path(path).resolve())

    if not allowed_roots:
        return resolved

    for root in allowed_roots:
        resolved_root = str(Path(root).resolve())
        if resolved == resolved_root or resolved.startswith(resolved_root + os.sep):
            return resolved

    raise IngestionSafetyError(
        f"Path '{path}' resolves to '{resolved}', which is outside every "
        f"configured ingestion_allowed_roots entry"
    )


def validate_git_url(url: str) -> None:
    """
    Validates a git URL's scheme and resolved host for SSRF safety.
    Raises `IngestionSafetyError` if the scheme isn't https/ssh, the host
    can't be resolved, or any resolved address is
    private/loopback/link-local/reserved/multicast/unspecified.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_GIT_URL_SCHEMES:
        raise IngestionSafetyError(
            f"Unsupported git URL scheme '{parsed.scheme}' -- only "
            f"{'/'.join(ALLOWED_GIT_URL_SCHEMES)} are allowed"
        )

    host = parsed.hostname
    if not host:
        raise IngestionSafetyError(f"Git URL '{url}' has no resolvable host")

    try:
        resolved = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise IngestionSafetyError(f"Could not resolve host '{host}': {e}") from e

    for family, _, _, _, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise IngestionSafetyError(
                f"Host '{host}' resolves to '{sockaddr[0]}', a "
                f"private/loopback/link-local/reserved address -- rejected "
                f"as an SSRF risk"
            )
