from __future__ import annotations

import json
import os
from pathlib import Path
import tomllib
from typing import Any, Optional
from pydantic import BaseModel, Field, model_validator


class CodePulseConfig(BaseModel):
    """
    Central configuration model for CodePulse.
    
    Precedence order:
      CLI flags > Environment variables (CODEPULSE_*) > codepulse.toml > Built-in defaults
    """

    # Storage & Database
    db_url: str = Field(
        default="sqlite:///codepulse.db",
        description="Database connection URL (e.g., sqlite:///codepulse.db)",
    )

    # Embedding Settings
    embedding_model: str = Field(
        default="all-MiniLM-L6-v2",
        description="SentenceTransformers model name for dense code embeddings",
    )
    embedding_device: str = Field(
        default="cpu",
        description="Inference compute device for embedding model (cpu, cuda, mps)",
    )
    embedding_dimension: int = Field(
        default=384,
        description="Dense vector embedding dimension",
    )

    # Chunker Settings
    chunk_max_tokens: int = Field(
        default=300,
        description="Approximate max tokens per code chunk",
    )
    chunk_overlap_lines: int = Field(
        default=3,
        description="Line overlap between sequential body chunks",
    )

    # Search & Ranking Settings
    weight_keyword: float = Field(
        default=0.40,
        description="Weight for BM25 keyword score in hybrid search fusion",
    )
    weight_vector: float = Field(
        default=0.40,
        description="Weight for vector similarity score in hybrid search fusion",
    )
    weight_graph: float = Field(
        default=0.20,
        description="Weight for call graph centrality score in hybrid search fusion",
    )
    exact_match_boost: float = Field(
        default=0.15,
        description="Score boost added when query exactly matches symbol name",
    )
    bm25_k1: float = Field(
        default=1.5,
        description="BM25 k1 term frequency saturation parameter",
    )
    bm25_b: float = Field(
        default=0.75,
        description="BM25 b document length normalization parameter",
    )
    rrf_k: int = Field(
        default=60,
        description=(
            "Reciprocal Rank Fusion (RRF) smoothing constant (roadmap §37 "
            "item 2.3: RRF fusion path wired to be selectable via API "
            "search_type/config). Formula: RRF_score(d) = sum(1 / (k + "
            "rank_i(d))). 60 is the conventional default used in the "
            "original RRF paper and most implementations -- not a "
            "benchmarked figure for this project specifically; tune via "
            "config/env for a given workload."
        ),
    )
    vector_floor_rank: int = Field(
        default=20,
        ge=0,
        description=(
            "How the semantic score is scaled before hybrid fusion. The "
            "lexical score is relative (the best keyword hit is always 1.0) "
            "but a raw cosine similarity is absolute and sits in a narrow "
            "band, so the two were weighted as if on one scale. The vector "
            "score is now (sim - floor) / (top - floor), clipped to 0..1, "
            "where top is the best similarity for the query and floor is the "
            "similarity of the hit at this rank. It does not depend on how "
            "many hits were fetched beyond this rank, so scores never change "
            "with the requested limit. 0 restores the old absolute "
            "(cosine + 1) / 2 scaling. 20 was chosen on paraphrase queries "
            "over two codebases (FUTURE_CHANGES item 20); not a universal "
            "constant."
        ),
    )

    # Server / API Settings
    server_host: str = Field(
        default="127.0.0.1",
        description="API server bind host",
    )
    server_port: int = Field(
        default=8000,
        description="API server bind port",
    )
    cors_origins: list[str] = Field(
        default_factory=list,
        description=(
            "Allowed CORS origin list. Empty by default (deny all cross-origin "
            "browser requests) -- Phase 0.9 replaces the prior '*' wildcard "
            "default, which combined with allow_credentials=True was both an "
            "unconsidered default and, per the CORS spec, a combination "
            "browsers reject outright for credentialed requests. Set explicit "
            "origin(s) via codepulse.toml's [server] section or the "
            "CODEPULSE_CORS_ORIGINS env var for deployments that need "
            "browser-based cross-origin access (e.g. a future Web UI)."
        ),
    )
    cors_allow_credentials: bool = Field(
        default=False,
        description="Whether to allow credentials in CORS headers. Defaults to False (secure by default).",
    )

    # Rate Limiting (Phase 1.4)
    rate_limit_enabled: bool = Field(
        default=True,
        description="Whether the in-process token-bucket rate-limiting middleware is active.",
    )
    rate_limit_requests_per_second: float = Field(
        default=5.0,
        description=(
            "In-process token-bucket rate limiter refill rate (tokens/second) "
            "per client identity (the raw API key header value, or the "
            "client's IP address as a fallback for unauthenticated "
            "requests). A reasonable starting default for local-first, "
            "single-developer use -- NOT a benchmarked/measured figure (see "
            "roadmap §19's own caution against presenting unvalidated "
            "numbers as fact); tune via config/env for a given workload."
        ),
    )
    rate_limit_burst: int = Field(
        default=20,
        description="Token-bucket capacity (max burst size) for the in-process rate limiter.",
    )

    # Ingestion Safety (Phase 1.5)
    ingestion_allowed_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Allow-list of root directories local-path ingestion (POST "
            "/v1/repositories/index) is restricted to. Empty by default -- "
            "meaning UNRESTRICTED -- deliberately preserving the roadmap's "
            "own local-first default experience ('codepulse index .' works "
            "with zero configuration): this endpoint acts on behalf of the "
            "local user who already has filesystem access to their own "
            "machine, so there is nothing to protect against until this API "
            "is exposed beyond localhost with a key untrusted parties might "
            "hold (roadmap §15's stated concern for this setting). Configure "
            "one or more roots via codepulse.toml/the "
            "CODEPULSE_INGESTION_ALLOWED_ROOTS env var to restrict ingestion "
            "for a server/team deployment; when set, a request path that "
            "resolves (after following symlinks and collapsing '..') outside "
            "every configured root is rejected."
        ),
    )
    ingestion_max_files_per_repo: int = Field(
        default=50000,
        description=(
            "Maximum number of discoverable files a single indexing run will "
            "accept before refusing to proceed (roadmap §15: 'max files per "
            "repo'). A generous starting ceiling, NOT a benchmarked figure -- "
            "tune via config for your workload."
        ),
    )
    ingestion_max_file_size_bytes: int = Field(
        default=5_000_000,
        description=(
            "Maximum size, in bytes, of a single file that will be read and "
            "parsed during indexing (roadmap §15: 'max file size parsed'). A "
            "larger file is skipped (logged, not fatal to the run -- the "
            "same per-file error-tolerance already established for "
            "malformed/unparseable files), not truncated. A reasonable "
            "starting default, NOT a benchmarked figure."
        ),
    )

    # Incremental Indexing (Phase 3.1)
    incremental_indexing_enabled: bool = Field(
        default=True,
        description=(
            "Whether IndexingEngine.index_directory() attempts git-diff-based "
            "incremental change detection (roadmap §37 item 3.1) before "
            "falling back to a full re-index. Every §9 fallback condition "
            "(not a git repo, no prior snapshot, embedding model changed, "
            "too large a fraction of files changed) still applies even when "
            "this is True; setting it False unconditionally forces a full "
            "re-index every time, matching this project's pre-Phase-3.1 "
            "behavior exactly."
        ),
    )
    incremental_max_changed_file_fraction: float = Field(
        default=0.5,
        description=(
            "If more than this fraction of a repository's previously-"
            "indexed files changed since the last snapshot, a full "
            "re-index is used instead of an incremental one (roadmap §9: "
            "'more than some threshold (e.g., 50%) of files changed at "
            "once (rebase, mass reformat) -- in which case a full "
            "re-index is actually cheaper than incremental bookkeeping'). "
            "0.5 is the roadmap's own literal example figure, not an "
            "independently benchmarked one."
        ),
    )

    model_config = {
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _validate_cors_policy(self) -> "CodePulseConfig":
        """
        Permanently rejects the invalid/insecure combination of a wildcard
        CORS origin with credentials enabled (Phase 0.9) -- browsers
        themselves refuse to honor credentialed requests against a '*'
        origin, so allowing this combination to be configured at all would
        only silently produce a broken (and misleadingly permissive-looking)
        policy. This guards the setting itself, not just its default, so a
        future config/env override can't reintroduce it unnoticed.
        """
        if self.cors_allow_credentials and "*" in self.cors_origins:
            raise ValueError(
                "Invalid CORS configuration: cors_allow_credentials=True cannot be "
                "combined with a wildcard ('*') in cors_origins -- browsers reject "
                "this combination for credentialed requests. Configure specific "
                "origin(s) in cors_origins, or set cors_allow_credentials=False."
            )
        return self


def _normalize_toml_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Normalizes a parsed TOML dictionary into CodePulseConfig field keys.
    Supports both flat keys (e.g. `db_url = "..."`) and sectioned tables
    (e.g. `[storage] db_url = "..."`).
    """
    normalized: dict[str, Any] = {}

    # 1. Flat top-level keys
    for k, v in raw.items():
        if not isinstance(v, dict):
            normalized[k] = v

    # 2. Sectioned tables
    storage = raw.get("storage", {})
    if isinstance(storage, dict):
        if "db_url" in storage:
            normalized["db_url"] = storage["db_url"]

    embedding = raw.get("embedding", {})
    if isinstance(embedding, dict):
        if "model" in embedding or "embedding_model" in embedding or "model_name" in embedding:
            normalized["embedding_model"] = (
                embedding.get("embedding_model")
                or embedding.get("model_name")
                or embedding.get("model")
            )
        if "device" in embedding or "embedding_device" in embedding:
            normalized["embedding_device"] = embedding.get("embedding_device") or embedding.get("device")
        if "dimension" in embedding or "embedding_dimension" in embedding:
            normalized["embedding_dimension"] = (
                embedding.get("embedding_dimension") or embedding.get("dimension")
            )

    chunker = raw.get("chunker", {})
    if isinstance(chunker, dict):
        if "max_tokens" in chunker or "chunk_max_tokens" in chunker:
            normalized["chunk_max_tokens"] = chunker.get("chunk_max_tokens") or chunker.get("max_tokens")
        if "overlap_lines" in chunker or "chunk_overlap_lines" in chunker:
            normalized["chunk_overlap_lines"] = (
                chunker.get("chunk_overlap_lines") or chunker.get("overlap_lines")
            )

    search = raw.get("search", {})
    if isinstance(search, dict):
        for key in (
            "weight_keyword",
            "weight_vector",
            "weight_graph",
            "exact_match_boost",
            "bm25_k1",
            "bm25_b",
            "rrf_k",
            "vector_floor_rank",
        ):
            if key in search:
                normalized[key] = search[key]

    server = raw.get("server", {})
    if isinstance(server, dict):
        if "host" in server or "server_host" in server:
            normalized["server_host"] = server.get("server_host") or server.get("host")
        if "port" in server or "server_port" in server:
            normalized["server_port"] = server.get("server_port") or server.get("port")
        if "cors_origins" in server:
            normalized["cors_origins"] = server["cors_origins"]
        if "cors_allow_credentials" in server:
            normalized["cors_allow_credentials"] = server["cors_allow_credentials"]

    return normalized


# Mapping of environment variables to CodePulseConfig field names
_ENV_VAR_MAPPINGS: dict[str, str] = {
    "CODEPULSE_DB_URL": "db_url",
    "CODEPULSE_EMBEDDING_MODEL": "embedding_model",
    "CODEPULSE_MODEL_NAME": "embedding_model",
    "CODEPULSE_EMBEDDING_DEVICE": "embedding_device",
    "CODEPULSE_DEVICE": "embedding_device",
    "CODEPULSE_EMBEDDING_DIMENSION": "embedding_dimension",
    "CODEPULSE_CHUNK_MAX_TOKENS": "chunk_max_tokens",
    "CODEPULSE_CHUNK_OVERLAP_LINES": "chunk_overlap_lines",
    "CODEPULSE_WEIGHT_KEYWORD": "weight_keyword",
    "CODEPULSE_WEIGHT_VECTOR": "weight_vector",
    "CODEPULSE_WEIGHT_GRAPH": "weight_graph",
    "CODEPULSE_EXACT_MATCH_BOOST": "exact_match_boost",
    "CODEPULSE_BM25_K1": "bm25_k1",
    "CODEPULSE_BM25_B": "bm25_b",
    "CODEPULSE_RRF_K": "rrf_k",
    "CODEPULSE_VECTOR_FLOOR_RANK": "vector_floor_rank",
    "CODEPULSE_SERVER_HOST": "server_host",
    "CODEPULSE_HOST": "server_host",
    "CODEPULSE_SERVER_PORT": "server_port",
    "CODEPULSE_PORT": "server_port",
    "CODEPULSE_CORS_ORIGINS": "cors_origins",
    "CODEPULSE_CORS_ALLOW_CREDENTIALS": "cors_allow_credentials",
    "CODEPULSE_RATE_LIMIT_ENABLED": "rate_limit_enabled",
    "CODEPULSE_RATE_LIMIT_REQUESTS_PER_SECOND": "rate_limit_requests_per_second",
    "CODEPULSE_RATE_LIMIT_BURST": "rate_limit_burst",
    "CODEPULSE_INGESTION_ALLOWED_ROOTS": "ingestion_allowed_roots",
    "CODEPULSE_INGESTION_MAX_FILES_PER_REPO": "ingestion_max_files_per_repo",
    "CODEPULSE_INGESTION_MAX_FILE_SIZE_BYTES": "ingestion_max_file_size_bytes",
    "CODEPULSE_INCREMENTAL_INDEXING_ENABLED": "incremental_indexing_enabled",
    "CODEPULSE_INCREMENTAL_MAX_CHANGED_FILE_FRACTION": "incremental_max_changed_file_fraction",
}


def _coerce_env_val(field_name: str, raw_val: str) -> Any:
    """Coerces a string environment variable value into its target field type."""
    field_info = CodePulseConfig.model_fields.get(field_name)
    if not field_info:
        return raw_val

    annotation = field_info.annotation

    if annotation is int:
        return int(raw_val)
    elif annotation is float:
        return float(raw_val)
    elif annotation is bool:
        return raw_val.strip().lower() in ("true", "1", "yes", "on")
    elif annotation is list[str] or getattr(annotation, "__origin__", None) is list:
        val_str = raw_val.strip()
        if val_str.startswith("[") and val_str.endswith("]"):
            try:
                return json.loads(val_str)
            except Exception:
                pass
        return [item.strip() for item in val_str.split(",") if item.strip()]

    return raw_val


def _load_env_overrides() -> dict[str, Any]:
    """Extracts all matching CODEPULSE_* environment variables."""
    overrides: dict[str, Any] = {}
    for env_key, field_name in _ENV_VAR_MAPPINGS.items():
        if env_key in os.environ:
            val = os.environ[env_key]
            overrides[field_name] = _coerce_env_val(field_name, val)
    return overrides


def load_config(
    config_file: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> CodePulseConfig:
    """
    Loads CodePulse configuration following the strict precedence rule:
    
      CLI flags > Environment variables > codepulse.toml > Built-in defaults
    """
    # 1. Start with empty dict (will use model defaults)
    merged: dict[str, Any] = {}

    # 2. Locate and load codepulse.toml if present
    toml_path: Optional[Path] = None
    if config_file:
        p = Path(config_file)
        if p.is_file():
            toml_path = p
    elif "CODEPULSE_CONFIG" in os.environ:
        p = Path(os.environ["CODEPULSE_CONFIG"])
        if p.is_file():
            toml_path = p
    else:
        # Check standard default locations
        cwd_toml = Path("codepulse.toml")
        if cwd_toml.is_file():
            toml_path = cwd_toml

    if toml_path and toml_path.is_file():
        try:
            with open(toml_path, "rb") as f:
                raw_toml = tomllib.load(f)
            toml_data = _normalize_toml_dict(raw_toml)
            merged.update(toml_data)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to parse TOML config at %s: %s", toml_path, e)

    # 3. Environment variable overrides (higher precedence than TOML)
    env_overrides = _load_env_overrides()
    merged.update(env_overrides)

    # 4. CLI overrides (highest precedence)
    if cli_overrides:
        for k, v in cli_overrides.items():
            if v is not None:
                merged[k] = v

    return CodePulseConfig(**merged)


_GLOBAL_SETTINGS: Optional[CodePulseConfig] = None


def get_settings(
    config_file: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    reload: bool = False,
) -> CodePulseConfig:
    """
    Returns the active CodePulse configuration singleton.
    If `reload=True` or explicit overrides are provided, reloads the configuration.
    """
    global _GLOBAL_SETTINGS
    if _GLOBAL_SETTINGS is None or reload or config_file is not None or cli_overrides is not None:
        _GLOBAL_SETTINGS = load_config(config_file=config_file, cli_overrides=cli_overrides)
    return _GLOBAL_SETTINGS


def set_settings(config: CodePulseConfig) -> None:
    """Explicitly sets the global configuration instance (primarily for testing)."""
    global _GLOBAL_SETTINGS
    _GLOBAL_SETTINGS = config


def reset_settings() -> None:
    """Resets the global configuration instance to uninitialized state."""
    global _GLOBAL_SETTINGS
    _GLOBAL_SETTINGS = None
