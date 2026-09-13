import os
import tempfile
from pathlib import Path
import pytest

from codepulse.config import (
    CodePulseConfig,
    get_settings,
    load_config,
    reset_settings,
    set_settings,
)
from codepulse.embeddings.chunker import SemanticChunker
from codepulse.embeddings.embedder import CodeEmbedder
from codepulse.search.bm25 import BM25Index
from codepulse.search.ranker import HybridRanker
from codepulse.storage.db import StorageManager


@pytest.fixture(autouse=True)
def clean_config_and_env(monkeypatch):
    """Ensure clean environment and reset global settings before and after each test."""
    reset_settings()
    for key in list(os.environ.keys()):
        if key.startswith("CODEPULSE_"):
            monkeypatch.delenv(key, raising=False)
    yield
    reset_settings()


def test_builtin_defaults_loaded():
    """
    1. Verify that built-in defaults are loaded when no TOML, env vars, or
    CLI overrides exist.

    Phase 0.9: cors_origins/cors_allow_credentials defaults updated from
    the prior `["*"]` / `True` (an unconsidered, spec-invalid combination
    for credentialed requests -- roadmap §3-C.3) to a secure-by-default
    `[]` / `False`. This assertion was updated to match; it is not a
    weakened test -- it now encodes the roadmap-required default instead
    of the fabricated/insecure one.
    """
    config = load_config(config_file="/nonexistent/path/to/codepulse.toml")
    assert config.db_url == "sqlite:///codepulse.db"
    assert config.embedding_model == "all-MiniLM-L6-v2"
    assert config.embedding_device == "cpu"
    assert config.embedding_dimension == 384
    assert config.chunk_max_tokens == 300
    assert config.chunk_overlap_lines == 3
    assert config.weight_keyword == 0.40
    assert config.weight_vector == 0.40
    assert config.weight_graph == 0.20
    assert config.exact_match_boost == 0.15
    assert config.bm25_k1 == 1.5
    assert config.bm25_b == 0.75
    assert config.rrf_k == 60
    assert config.server_host == "127.0.0.1"
    assert config.server_port == 8000
    assert config.cors_origins == []
    assert config.cors_allow_credentials is False
    # Phase 1.4: in-process token-bucket rate limiter defaults.
    assert config.rate_limit_enabled is True
    assert config.rate_limit_requests_per_second == 5.0
    assert config.rate_limit_burst == 20
    # Phase 1.5: ingestion safety defaults (unrestricted allow-list by
    # design -- see CodePulseConfig.ingestion_allowed_roots's docstring).
    assert config.ingestion_allowed_roots == []
    assert config.ingestion_max_files_per_repo == 50000
    assert config.ingestion_max_file_size_bytes == 5_000_000


def test_toml_overrides_defaults():
    """2. Verify that codepulse.toml overrides built-in defaults (both flat and sectioned format)."""
    toml_content = """
    db_url = "sqlite:///custom_flat.db"

    [embedding]
    model = "custom-code-model"
    device = "cuda"
    dimension = 768

    [chunker]
    max_tokens = 500
    overlap_lines = 5

    [search]
    weight_keyword = 0.50
    weight_vector = 0.30
    weight_graph = 0.20
    exact_match_boost = 0.25
    bm25_k1 = 1.8
    bm25_b = 0.80
    rrf_k = 40

    [server]
    host = "0.0.0.0"
    port = 9000
    cors_origins = ["https://app.codepulse.dev"]
    cors_allow_credentials = false
    """

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        temp_path = f.name

    try:
        config = load_config(config_file=temp_path)
        assert config.db_url == "sqlite:///custom_flat.db"
        assert config.embedding_model == "custom-code-model"
        assert config.embedding_device == "cuda"
        assert config.embedding_dimension == 768
        assert config.chunk_max_tokens == 500
        assert config.chunk_overlap_lines == 5
        assert config.weight_keyword == 0.50
        assert config.weight_vector == 0.30
        assert config.weight_graph == 0.20
        assert config.exact_match_boost == 0.25
        assert config.bm25_k1 == 1.8
        assert config.bm25_b == 0.80
        assert config.rrf_k == 40
        assert config.server_host == "0.0.0.0"
        assert config.server_port == 9000
        assert config.cors_origins == ["https://app.codepulse.dev"]
        assert config.cors_allow_credentials is False
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_environment_variables_override_toml(monkeypatch):
    """3. Verify that environment variables override codepulse.toml values."""
    toml_content = """
    db_url = "sqlite:///from_toml.db"
    server_port = 8000
    """

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        temp_path = f.name

    try:
        monkeypatch.setenv("CODEPULSE_DB_URL", "sqlite:///from_env.db")
        monkeypatch.setenv("CODEPULSE_SERVER_PORT", "9999")
        monkeypatch.setenv("CODEPULSE_EMBEDDING_MODEL", "env-model-v1")
        monkeypatch.setenv("CODEPULSE_RRF_K", "25")

        config = load_config(config_file=temp_path)
        assert config.db_url == "sqlite:///from_env.db"
        assert config.server_port == 9999
        assert config.embedding_model == "env-model-v1"
        assert config.rrf_k == 25
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_cli_flags_override_environment(monkeypatch):
    """4. Verify that CLI overrides override environment variables."""
    monkeypatch.setenv("CODEPULSE_DB_URL", "sqlite:///from_env.db")
    monkeypatch.setenv("CODEPULSE_SERVER_PORT", "9999")

    cli_overrides = {
        "db_url": "sqlite:///from_cli.db",
        "server_port": 7777,
    }

    config = load_config(cli_overrides=cli_overrides)
    assert config.db_url == "sqlite:///from_cli.db"
    assert config.server_port == 7777


def test_full_precedence_chain(monkeypatch):
    """
    5. Verify the full precedence chain:
       CLI > Environment > TOML > Defaults
    """
    toml_content = """
    db_url = "sqlite:///toml_db.db"
    embedding_model = "toml-model"
    server_port = 8080
    chunk_max_tokens = 400
    """

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_content)
        temp_path = f.name

    try:
        # Set Env vars for db_url and embedding_model
        monkeypatch.setenv("CODEPULSE_DB_URL", "sqlite:///env_db.db")
        monkeypatch.setenv("CODEPULSE_EMBEDDING_MODEL", "env-model")

        # Set CLI override ONLY for db_url
        cli_overrides = {
            "db_url": "sqlite:///cli_db.db",
        }

        config = load_config(config_file=temp_path, cli_overrides=cli_overrides)

        # 1. db_url: Set in Defaults, TOML, Env, and CLI -> CLI wins
        assert config.db_url == "sqlite:///cli_db.db"

        # 2. embedding_model: Set in Defaults, TOML, and Env -> Env wins
        assert config.embedding_model == "env-model"

        # 3. server_port: Set in Defaults and TOML -> TOML wins
        assert config.server_port == 8080
        assert config.chunk_max_tokens == 400

        # 4. weight_keyword: Only in Defaults -> Defaults win
        assert config.weight_keyword == 0.40
        assert config.bm25_k1 == 1.5
        assert config.rrf_k == 60
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_env_var_type_coercion(monkeypatch):
    """Verify that boolean, int, float, and comma-separated list env vars are coerced properly."""
    monkeypatch.setenv("CODEPULSE_SERVER_PORT", "9090")
    monkeypatch.setenv("CODEPULSE_WEIGHT_KEYWORD", "0.65")
    monkeypatch.setenv("CODEPULSE_CORS_ALLOW_CREDENTIALS", "false")
    monkeypatch.setenv("CODEPULSE_CORS_ORIGINS", "https://foo.com, https://bar.com")
    monkeypatch.setenv("CODEPULSE_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("CODEPULSE_RATE_LIMIT_REQUESTS_PER_SECOND", "12.5")
    monkeypatch.setenv("CODEPULSE_RATE_LIMIT_BURST", "50")
    monkeypatch.setenv("CODEPULSE_INGESTION_ALLOWED_ROOTS", "/repos/allowed, /repos/also-allowed")
    monkeypatch.setenv("CODEPULSE_INGESTION_MAX_FILES_PER_REPO", "1234")
    monkeypatch.setenv("CODEPULSE_INGESTION_MAX_FILE_SIZE_BYTES", "999")

    config = load_config()
    assert config.server_port == 9090
    assert config.weight_keyword == 0.65
    assert config.cors_allow_credentials is False
    assert config.cors_origins == ["https://foo.com", "https://bar.com"]
    assert config.rate_limit_enabled is False
    assert config.rate_limit_requests_per_second == 12.5
    assert config.rate_limit_burst == 50
    assert config.ingestion_allowed_roots == ["/repos/allowed", "/repos/also-allowed"]
    assert config.ingestion_max_files_per_repo == 1234
    assert config.ingestion_max_file_size_bytes == 999


def test_components_use_configured_settings(monkeypatch):
    """Verify that CodePulse components automatically adopt settings from get_settings()."""
    custom_config = CodePulseConfig(
        db_url="sqlite:///:memory:",
        embedding_model="custom-test-model",
        embedding_device="cpu",
        embedding_dimension=256,
        chunk_max_tokens=150,
        chunk_overlap_lines=1,
        weight_keyword=0.60,
        weight_vector=0.25,
        weight_graph=0.15,
        exact_match_boost=0.30,
        bm25_k1=2.0,
        bm25_b=0.90,
        rrf_k=15,
    )
    set_settings(custom_config)

    # 1. StorageManager
    storage = StorageManager()
    assert storage.db_url == "sqlite:///:memory:"

    # 2. CodeEmbedder
    embedder = CodeEmbedder()
    assert embedder.model_name == "custom-test-model"
    assert embedder.dimension == 256

    # 3. SemanticChunker
    chunker = SemanticChunker()
    assert chunker.max_tokens == 150
    assert chunker.chunk_overlap_lines == 1

    # 4. HybridRanker
    ranker = HybridRanker()
    assert ranker.weight_keyword == 0.60
    assert ranker.weight_vector == 0.25
    assert ranker.weight_graph == 0.15
    assert ranker.exact_match_boost == 0.30

    # 5. BM25Index
    bm25 = BM25Index()
    assert bm25.k1 == 2.0
    assert bm25.b == 0.90

    # 6. HybridRanker's rrf_k (Phase 2.3)
    assert ranker.rrf_k == 15
