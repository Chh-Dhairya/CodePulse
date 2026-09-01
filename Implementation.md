# Project Title

CodePulse: Semantic Codebase Intelligence Engine

## Problem Statement

Developers spend 30-40% of time understanding unfamiliar code. Code search tools are either:

1. Text-only (grep/regex) - no semantic understanding
2. Centralized cloud (GitHub Copilot) - vendor lock-in, privacy concerns
3. Academic (no production reliability)

**Solution**: Build a production-grade semantic code search engine that indexes repositories, performs hybrid retrieval (BM25 keyword + dense embeddings + symbol call graph), and exposes a low-latency API for IDE integration and AI agents.

## Target Users
- **Individual developers**: IDE extension for personal & local projects
- **Small teams**: Self-hosted codebase search and navigation
- **AI agents**: Fast API for autonomous code comprehension and context injection
- **Code analysis tools**: Integration with linters, formatters, and CI/CD pipelines

## Key Features
- **Modular Multi-Language Parsing (`tree-sitter-languages`)**: Pluggable AST parser starting with Python & JS/TS (covering 70%+ of users), expanding to Go, Rust, and Java.
- **Hybrid Search**: Combine embedded BM25 (Tantivy) + dense vector embeddings (HNSW in pgvector) + symbol call graph.
- **Low-Latency API**: <150ms p95 response time for 1M+ file repositories.
- **Incremental Git-Diff Indexing**: Efficient diff-based updates for changed symbols, embeddings, and call graph edges.
- **Scoped Call Graph & Impact Analysis**: Phased import resolution and acyclic call dependency traversal.
- **IDE Integration**: VS Code extension with instant symbol lookups and context search.
- **Rate Limiting & Backpressure**: Token bucket rate limits with explicit queue depth rejection (HTTP 429).
- **Security & Privacy**: Per-team code isolation, self-hosted deployment, PII detection.
- **Monitoring**: Prometheus metrics, structured health checks, latency dashboards.

---

## ⚠️ High-Risk Design Decisions & Mitigations

### 1. Graph Complexity Without Clear Payoff
- **Problem**: Full multi-language call graph and impact analysis is deceptively complex:
  - Import resolution is non-trivial (aliasing, relative imports, dynamic imports).
  - Call graph construction is difficult (dynamic method dispatch, inheritance, overloading).
  - Impact analysis involves cycle detection and reachability in complex cyclic graphs.
- **Risk**: Spending 3+ weeks on graph logic before validating if users primarily want fast, high-accuracy keyword and semantic code search.
- **Mitigation & Phased Approach**:
  - **Phase 2A (1 week)**: Build basic import resolution for **Python only**.
  - **Phase 2B (2 weeks)**: Add call graphs, but strictly scope to **acyclic callables first**.
  - **Validation / Metric**: Conduct developer interviews and telemetry analysis to validate impact analysis demand before building complex multi-language call graphs.

### 2. Multi-Language Scope Phasing (Avoid 5 Languages on Day 1)
- **Problem**: Supporting Python, JS/TS, Go, Rust, and Java from Day 1 introduces a 5x parser testing burden, 5 distinct scoping/AST models, and high maintenance overhead.
- **Mitigation & Phased Rollout**:
  - **Phase 1 (MVP)**: Python + JavaScript/TypeScript only (covers ~70% of developer workflows).
  - **Phase 5**: Add Go + Rust (after core parser abstractions and storage patterns are proven).
  - **Phase 8**: Add Java (if enterprise demand exists).
- **Modular Parser Architecture**:
  ```
  codepulse/parser/
  ├── base.py               # LanguageParser ABC interface
  ├── python.py             # PythonParser implementation
  ├── javascript.py         # JavaScriptParser implementation
  └── grammars/             # Tree-sitter query (.scm) files
      ├── python.scm        # Symbol capture queries
      └── javascript.scm    # JS/TS capture queries
  ```

### 3. Embedding Model Selection & Benchmarking
- **Problem**: Choosing `all-MiniLM-L6-v2` vs `CodeBERT` (or `bge-small-en-v1.5`) without empirical domain validation is risky:
  | Model | Dimensions | Latency (CPU) | Code Search Accuracy | Size | Notes |
  |---|---|---|---|---|---|
  | **all-MiniLM-L6-v2** | 384 | ~5ms | ~65% | ~60MB | Ultra-fast, lightweight, general text |
  | **CodeBERT** | 768 | ~15ms | ~82% | ~300MB | Code-specialized, higher accuracy, higher compute |
- **Mitigation & Benchmark Plan**:
  - **Phase 3A (3 days)**: Build an automated **Embedding Benchmark Suite**.
  - Evaluate both models across a benchmark of 1,000 real code search queries collected from target developers and repos.
  - Decide final production default based on measured accuracy vs latency trade-offs rather than assumptions.

---

## Tech Stack Specifications

### 1. Storage Layer
- **Production Relational & Vector DB**: **PostgreSQL 16+ with `pgvector` extension**
  - Stores files, AST symbols, metadata, call graphs, and dense code embeddings.
  - Managed options readily available (AWS RDS, Supabase, Neon, self-hosted).
- **Embedded Full-Text / Keyword Search**: **Tantivy** (Rust-based embedded search engine via Python bindings)
  - Native BM25 scoring with zero external server dependencies (eliminates heavy Elasticsearch/Java runtime overhead).
  - High indexing throughput (50MB/s+) and microsecond search latency.
- **Cache & Message Broker**: **Redis** (Single dependency)
  - In-memory response caching and query result caching (5-minute TTL).
  - Token-bucket rate limiting (sliding window).
  - Distributed job queue via **Redis Streams** with consumer groups.
- **Local & Development Deployments**: **SQLite** (Optional standalone dev mode)
  - File-backed SQLite for lightweight local developer setups with zero container prerequisites.

### 2. Search & Retrieval Engine
- **Dense Embedding Model**: **`all-MiniLM-L6-v2`** (Baseline, subject to Phase 3A benchmark vs `CodeBERT`)
  - 384 dimensions, compact footprint (~60MB), ultra-fast inference (~5ms per snippet on CPU).
  - Encodes function bodies, signatures, and docstrings.
- **Keyword Search (BM25)**: **Tantivy embedded index**
  - Code-aware tokenization, identifier splitting (camelCase/snake_case), and exact matching.
- **Vector Indexing Algorithm**: **HNSW (Hierarchical Navigable Small World)** via `pgvector`
  - `hnsw (embedding vector_cosine_ops)` for sub-10ms approximate nearest neighbor search with >95% recall.
- **Hybrid Retrieval Strategy**: Reciprocal Rank Fusion (RRF) & weighted combination of BM25 + Vector Cosine + Symbol Graph Proximity.

### 3. Background Jobs & Worker Pipeline
- **MVP / Single-Node**: **`asyncio.Queue`**
  - Lightweight, in-process asynchronous task queue for zero-dependency local indexing.
- **Horizontal Scaling**: **Redis Streams**
  - Consumer groups for distributed worker pools, at-least-once delivery, `XACK` confirmations, and dead-letter queues. Avoids heavy Celery/RabbitMQ/Kafka operational complexity.

### 4. Language Support & AST Parsing
- **AST Parser**: **`tree-sitter-languages`**
  - Modular parser architecture with pre-compiled universal binary wheels.
- **Dependency Management**: Pinned versions in `pyproject.toml` for strict build reproducibility.

---

## Architecture (Detailed)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              Client Layer                               │
├──────────────────────┬──────────────────────────┬───────────────────────┤
│    VS Code Ext       │      Web Dashboard       │  AI Agent API Client  │
└──────────┬───────────┴────────────┬─────────────┴───────────┬───────────┘
           │                        │                         │ (REST/WebSocket)
           └────────────────────────┼─────────────────────────┘
                                    │
        ┌───────────────────────────▼───────────────────────────┐
        │                 API Gateway (FastAPI)                 │
        │  - Auth (JWT / API Key)                               │
        │  - Token Bucket Rate Limiting (Redis)                 │
        │  - Backpressure Protection (Reject >1000 queue depth) │
        │  - Request validation (Pydantic v2)                   │
        │  - Response cache (Redis)                             │
        └───────────────────────────┬───────────────────────────┘
                                    │
            ┌───────────────────────┼───────────────────────┐
            │                       │                       │
     ┌──────▼───────┐        ┌──────▼───────┐        ┌──────▼────────┐
     │Search Service│        │Symbol Service│        │Job Dispatcher │
     │- Tantivy BM25│        │- PostgreSQL  │        │- asyncio.Queue│
     │- pgvector    │        │  (or SQLite) │        │- Redis Streams│
     └──────┬───────┘        └──────┬───────┘        └──────┬────────┘
            │                       │                       │
     ┌──────▼───────┐        ┌──────▼───────┐        ┌──────▼────────┐
     │Dense Vectors │        │Relational DB │        │Distributed    │
     │(pgvector     │        │(Files,       │        │Job Queue      │
     │ HNSW Index)  │        │ Symbols,     │        │(Redis Streams)│
     │              │        │ Call Graph)  │        │               │
     └──────────────┘        └──────────────┘        └──────┬────────┘
            │                       │                       │
            └───────────────────────┼───────────────────────┘
                                    │
                     ┌──────────────┴──────────────┐
                     │     Background Workers      │
                     │ - Modular LanguageParser    │
                     │ - Embedding Pipeline        │
                     │ - Incremental Git Diff Sync │
                     │ - Tantivy + pgvector Sync   │
                     └─────────────────────────────┘
```

---

## Data Flow & Algorithms

### 1. Full Initial Indexing Flow:
1. **Repository submitted**: API receives repository URL or local path.
2. **Job Enqueued**: Dispatched to `asyncio.Queue` (MVP) or `XADD` event in Redis Streams.
3. **Worker Pickup**: Background worker claims the indexing job from the queue.
4. **AST Parsing (`tree-sitter-languages`)**: Pluggable `LanguageParser` extracts functions, classes, signatures, and docstrings.
5. **Dense Embedding**: Generate dense embeddings for code chunks and docstrings (~5ms/symbol).
6. **Call Graph Construction**: Traverse AST references to compute caller/callee relationships.
7. **Storage Persistence**:
   - Store relational metadata & symbols in **PostgreSQL** (or SQLite).
   - Write dense vectors to **pgvector** table with **HNSW** index.
   - Index keywords, tokens, and docstrings into embedded **Tantivy** index.
8. **Cache & Status Update**: Update repository indexing stats in Redis.
9. **Notification**: Emit completion status via WebSocket / API status endpoint.

### 2. Incremental Indexing Algorithm (Phase 6 Spec):

```python
def incremental_index(repo_id: int, last_indexed_commit: str, current_commit: str, db_session, tantivy_index):
    """
    Performs precise incremental indexing using Git diffs:
    1. Identifies added, modified, and deleted files.
    2. Re-parses changed files and updates symbol metadata.
    3. Re-computes embeddings for changed/new symbols.
    4. Updates call graph edges via local BFS.
    5. Purges stale symbols and vectors.
    """
    # 1. Get changed files
    diff_output = run_git_command(f"git diff {last_indexed_commit}..{current_commit} --name-status")
    # e.g., [('M', 'app/auth.py'), ('A', 'app/tokens.py'), ('D', 'app/legacy.py')]
    
    modified_or_added = [path for status, path in diff_output if status in ('M', 'A')]
    deleted_files = [path for status, path in diff_output if status == 'D']

    # 2. Handle Deleted Files
    for file_path in deleted_files:
        stale_symbols = db_session.query(Symbol).filter_by(repo_id=repo_id, file_path=file_path).all()
        for sym in stale_symbols:
            db_session.delete(sym)  # Cascades to code_embeddings & dependencies
            tantivy_index.delete_document("symbol_id", sym.id)

    # 3. Process Added & Modified Files
    for file_path in modified_or_added:
        source_code = read_repo_file(repo_id, file_path)
        parser = get_parser_for_file(file_path)
        new_symbols = parser.extract_symbols(source_code)
        old_symbols = {s.name: s for s in db_session.query(Symbol).filter_by(repo_id=repo_id, file_path=file_path).all()}

        for new_sym in new_symbols:
            existing = old_symbols.get(new_sym["name"])
            
            # Heuristic match: check if signature or body changed
            if existing and existing.signature == new_sym["signature"] and existing.body_hash == new_sym["body_hash"]:
                # Symbol unchanged, skip embedding generation
                continue
            
            # Upsert symbol record
            sym_record = upsert_symbol_record(db_session, repo_id, file_path, new_sym)
            
            # Generate new embedding and update pgvector
            vec = generate_symbol_embedding(new_sym["signature"], new_sym["docstring"], new_sym["body"])
            upsert_embedding(db_session, sym_record.id, vec)
            
            # Update Tantivy inverted index
            tantivy_index.upsert_document(sym_record.id, new_sym)

        # 4. Update Call Graph Edges (BFS from changed symbols)
        update_file_call_graph(db_session, repo_id, file_path, new_symbols)

    db_session.commit()
    tantivy_index.commit()
```

### 3. Search Flow:
1. **User Query**: Natural language question or code snippet received by API.
2. **Query Embedding**: Encode query vector (~5ms).
3. **Parallel Hybrid Retrieval**:
   - **BM25 Search (Tantivy)**: High-speed keyword and exact identifier match.
   - **Dense Vector Search (pgvector HNSW)**: Cosine similarity search (`<->`) in PostgreSQL.
   - **Symbol Exact Lookup**: Direct DB match for class/function names.
   - **Call Graph Expansion**: Proximity boost for related/frequently called symbols.
4. **Merge & Rank**: Combine scores via weighted linear combination or Reciprocal Rank Fusion (RRF).
5. **Return Response**: Return top-K ranked snippets with line numbers, file paths, and relevance scores.
6. **Cache**: Cache query response in Redis for 5 minutes.

---

## Database Schema & Indexing

### PostgreSQL Schema (with pgvector HNSW):

```sql
-- Enable vector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Repositories
CREATE TABLE repositories (
  id SERIAL PRIMARY KEY,
  team_id INT NOT NULL,
  url TEXT NOT NULL,
  branch TEXT DEFAULT 'main',
  last_indexed_commit VARCHAR(64),
  last_sync TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  file_count INT DEFAULT 0,
  symbol_count INT DEFAULT 0
);

-- Files
CREATE TABLE files (
  id BIGSERIAL PRIMARY KEY,
  repo_id INT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  language TEXT,
  size INT,
  last_indexed TIMESTAMP WITH TIME ZONE,
  UNIQUE(repo_id, path)
);

-- Symbols (functions, classes, methods)
CREATE TABLE symbols (
  id BIGSERIAL PRIMARY KEY,
  file_id BIGINT REFERENCES files(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  type VARCHAR(32) NOT NULL, -- 'function', 'class', 'module', 'interface'
  line_start INT NOT NULL,
  line_end INT NOT NULL,
  docstring TEXT,
  signature TEXT,
  body_hash VARCHAR(64),
  complexity INT DEFAULT 1
);

-- Dependencies (call graph)
CREATE TABLE dependencies (
  source_symbol_id BIGINT REFERENCES symbols(id) ON DELETE CASCADE,
  target_symbol_id BIGINT REFERENCES symbols(id) ON DELETE CASCADE,
  call_count INT DEFAULT 1,
  PRIMARY KEY (source_symbol_id, target_symbol_id)
);

-- Code Vector Embeddings (pgvector HNSW)
CREATE TABLE code_embeddings (
  id BIGSERIAL PRIMARY KEY,
  symbol_id BIGINT REFERENCES symbols(id) ON DELETE CASCADE,
  embedding vector(384), -- 384-dim (or 768-dim if CodeBERT selected)
  embedding_type VARCHAR(32) NOT NULL, -- 'signature', 'docstring', 'body', 'full'
  UNIQUE(symbol_id, embedding_type)
);

-- Indexes for Fast Querying
CREATE INDEX idx_files_repo ON files(repo_id);
CREATE INDEX idx_symbols_file ON symbols(file_id);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_dependencies_target ON dependencies(target_symbol_id);

-- HNSW Vector Index (Fast ANN Cosine Similarity)
CREATE INDEX idx_code_embeddings_hnsw 
ON code_embeddings 
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

### Tantivy Embedded Schema (Keyword Index):

```python
import tantivy

# Embedded Tantivy schema for zero-dependency keyword & BM25 search
schema_builder = tantivy.SchemaBuilder()
schema_builder.add_integer_field("symbol_id", stored=True, indexed=True)
schema_builder.add_integer_field("repo_id", stored=True, indexed=True)
schema_builder.add_text_field("name", stored=True, tokenizer_name="default")
schema_builder.add_text_field("docstring", stored=True, tokenizer_name="default")
schema_builder.add_text_field("source_code", stored=False, tokenizer_name="code")
schema_builder.add_text_field("file_path", stored=True, tokenizer_name="raw")

tantivy_schema = schema_builder.build()
```

---

## Gateway, Rate Limiting & Backpressure Specifications

### Rate Limits Specification (Phase 5):
Rate limits are enforced per API key / tenant via Redis Token Bucket:

```python
rate_limits = {
    "search": 100 / 60,         # 100 search requests per minute per API key
    "index": 10 / 60,           # 10 repository indexing jobs per minute per tenant
    "graph_analysis": 50 / 60,  # 50 impact analysis requests per minute
}
```

### Backpressure Strategy:
- **Queue Depth Inspection**: Background job queue depth in Redis Streams / `asyncio.Queue` is checked before accepting new jobs.
- **Explicit 429 Rejection**:
  - If pending queue depth exceeds **1,000 jobs**, new indexing requests are rejected immediately with HTTP `429 Too Many Requests` and a `Retry-After: 30` header.
  - **Rationale**: Prevents unbounded memory growth, worker saturation, and catastrophic tail-latency degradation during spike loads.

---

## APIs (12 Key Endpoints)

### 1. Search Endpoint
```http
POST /v1/search
Content-Type: application/json
Authorization: Bearer $TOKEN

{
  "repository_id": 123,
  "query": "database connection management",
  "limit": 20,
  "search_type": "hybrid"  // "keyword", "semantic", "hybrid"
}
```
**Response 200**:
```json
{
  "results": [
    {
      "symbol_id": 456,
      "name": "create_db_connection",
      "type": "function",
      "file": "db/connection.py",
      "line": 45,
      "language": "python",
      "relevance_score": 0.92,
      "snippet": "def create_db_connection(host, port):\n  # Creates PostgreSQL connection pool\n  return psycopg2.pool.SimpleConnectionPool(..)",
      "embedding_distance": 0.15,
      "keyword_score": 0.88
    }
  ],
  "execution_time_ms": 78
}
```

### 2. Index Repository
```http
POST /v1/repositories/index
Content-Type: application/json
Authorization: Bearer $TOKEN

{
  "repository_id": 123,
  "source": "github",
  "url": "https://github.com/user/repo.git",
  "branch": "main",
  "incremental": true
}
```
**Response 202**:
```json
{
  "job_id": "job_stream_1724345000_1",
  "status": "queued",
  "queue_backend": "redis_streams",
  "estimated_time_seconds": 120
}
```

### 3. Get Index Status
```http
GET /v1/repositories/123/index-status
Authorization: Bearer $TOKEN
```
**Response 200**:
```json
{
  "repository_id": 123,
  "indexed_files": 12534,
  "total_files": 12534,
  "indexed_symbols": 45821,
  "last_indexed": "2026-08-20T10:30:00Z",
  "embedding_model": "all-MiniLM-L6-v2 (384-dim)",
  "vector_index": "pgvector_hnsw",
  "keyword_index": "tantivy_embedded",
  "index_size_mb": 420
}
```

### 4. Get Symbol Details
```http
GET /v1/symbols/456
Authorization: Bearer $TOKEN
```

### 5. Find Similar Code
```http
POST /v1/symbols/456/similar
Content-Type: application/json

{
  "limit": 10,
  "threshold": 0.75
}
```

### 6. Analyze Impact (Call Graph)
```http
POST /v1/symbols/456/impact-analysis
Content-Type: application/json

{
  "depth": 3
}
```

### 7. Autocomplete
```http
GET /v1/autocomplete?repo_id=123&prefix=create_db&limit=10
Authorization: Bearer $TOKEN
```

### 8. Search within Function
```http
POST /v1/symbols/456/search
Content-Type: application/json

{
  "query": "connection pool",
  "search_in": "docstring"
}
```

### 9. List Symbols in File
```http
GET /v1/files/789/symbols?language_filter=function
Authorization: Bearer $TOKEN
```

### 10. Get Repository Stats
```http
GET /v1/repositories/123/stats
Authorization: Bearer $TOKEN
```

### 11. IDE Plugin Context Search
```http
POST /v1/ide-search
Content-Type: application/json

{
  "repository_id": 123,
  "selected_text": "find_user",
  "file_context": "models/user.py",
  "limit": 5
}
```

### 12. Search Relevance Feedback
```http
POST /v1/search/feedback
Content-Type: application/json

{
  "search_id": "search_xyz",
  "result_id": 456,
  "feedback": "relevant"
}
```

---

## AI/ML Architecture & Retrieval Implementation

### 1. Modular AST Parsing Architecture:

```python
# codepulse/parser/base.py
from abc import ABC, abstractmethod
from typing import Any

class LanguageParser(ABC):
    @abstractmethod
    def extract_symbols(self, source_code: str) -> list[dict[str, Any]]:
        """Extract functions, classes, signatures, and docstrings from source."""
        pass

    @abstractmethod
    def extract_imports(self, source_code: str) -> list[str]:
        """Extract imported modules and symbols."""
        pass
```

```python
# codepulse/parser/python.py
from tree_sitter_languages import get_parser
from codepulse.parser.base import LanguageParser

class PythonParser(LanguageParser):
    def __init__(self):
        self.parser = get_parser("python")

    def extract_symbols(self, source_code: str) -> list[dict]:
        source_bytes = bytes(source_code, "utf8")
        tree = self.parser.parse(source_bytes)
        symbols = []
        
        def walk(node):
            if node.type in ("function_definition", "class_definition"):
                name_node = node.child_by_field_name("name")
                name = source_bytes[name_node.start_byte:name_node.end_byte].decode("utf8") if name_node else "anonymous"
                symbols.append({
                    "name": name,
                    "type": "function" if node.type == "function_definition" else "class",
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "signature": self._get_signature(node, source_bytes),
                    "docstring": self._get_docstring(node, source_bytes),
                    "body": source_bytes[node.start_byte:node.end_byte].decode("utf8")
                })
            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return symbols

    def _get_signature(self, node, source_bytes) -> str:
        # Extract signature definition line
        return source_bytes[node.start_byte:node.end_byte].decode("utf8").split("\n")[0]

    def _get_docstring(self, node, source_bytes) -> str:
        # Extract triple-quoted docstrings if present
        body_node = node.child_by_field_name("body")
        if body_node and body_node.children:
            first_expr = body_node.children[0]
            if first_expr.type == "expression_statement" and first_expr.children:
                str_node = first_expr.children[0]
                if str_node.type == "string":
                    return source_bytes[str_node.start_byte:str_node.end_byte].decode("utf8").strip("\"'")
        return ""
```

### 2. Embedding Benchmark (Phase 3A):

```python
# scripts/benchmark_embeddings.py
import time
from sentence_transformers import SentenceTransformer

MODELS_TO_BENCHMARK = {
    "all-MiniLM-L6-v2": "all-MiniLM-L6-v2",
    "CodeBERT": "microsoft/codebert-base"
}

def benchmark_models(test_queries: list[str], test_code_corpus: list[str]):
    results = {}
    for name, model_path in MODELS_TO_BENCHMARK.items():
        model = SentenceTransformer(model_path)
        start = time.perf_counter()
        embeddings = model.encode(test_queries, batch_size=32, show_progress_bar=False)
        duration = time.perf_counter() - start
        
        avg_latency_ms = (duration / len(test_queries)) * 1000
        results[name] = {
            "avg_latency_ms": avg_latency_ms,
            "dimensions": embeddings.shape[1]
        }
    return results
```

### 3. Hybrid Search Execution (Tantivy + pgvector HNSW):

```python
def search_hybrid(user_query: str, repo_id: int, db_session, tantivy_index, model, limit: int = 20):
    # 1. Generate query embedding (~5ms)
    query_vector = model.encode(user_query, normalize_embeddings=True).tolist()

    # 2. Dense Vector Search via pgvector (HNSW index)
    vector_results = db_session.execute("""
        SELECT symbol_id, 1 - (embedding <=> :query_vec::vector) AS vector_similarity
        FROM code_embeddings
        WHERE symbol_id IN (
            SELECT id FROM symbols WHERE file_id IN (
                SELECT id FROM files WHERE repo_id = :repo_id
            )
        )
        ORDER BY embedding <=> :query_vec::vector ASC
        LIMIT :candidate_limit
    """, {
        "query_vec": str(query_vector),
        "repo_id": repo_id,
        "candidate_limit": limit * 2
    }).fetchall()

    # 3. Keyword Search via Tantivy (Embedded BM25)
    searcher = tantivy_index.searcher()
    query_parser = tantivy.QueryParser(tantivy_index.schema, ["name", "docstring", "source_code"])
    tantivy_query = query_parser.parse_query(user_query)
    bm25_hits = searcher.search(tantivy_query, limit * 2).hits

    bm25_results = {
        searcher.doc(doc_address)["symbol_id"][0]: score
        for score, doc_address in bm25_hits
    }

    # 4. Merge via Weighted Combination / Reciprocal Rank Fusion
    ranked = merge_and_rank(vector_results, bm25_results)
    return ranked[:limit]

def merge_and_rank(vector_results, bm25_results, alpha=0.5):
    combined_scores = {}
    for symbol_id, vec_sim in vector_results:
        combined_scores[symbol_id] = alpha * vec_sim

    max_bm25 = max(bm25_results.values()) if bm25_results else 1.0
    for symbol_id, bm25_score in bm25_results.items():
        norm_bm25 = bm25_score / (max_bm25 or 1.0)
        combined_scores[symbol_id] = combined_scores.get(symbol_id, 0) + (1 - alpha) * norm_bm25

    return sorted(combined_scores.items(), key=lambda x: x[1], reverse=True)
```

---

## Dependency Specification (`pyproject.toml`)

```toml
[project]
name = "codepulse"
version = "0.1.0"
description = "Semantic Codebase Intelligence Engine"
requires-python = ">=3.11,<3.13"
dependencies = [
    # API & Web Framework
    "fastapi>=0.111.0,<0.112.0",
    "uvicorn[standard]>=0.30.0,<0.31.0",
    "pydantic>=2.7.0,<2.8.0",
    "pydantic-settings>=2.3.0,<2.4.0",

    # Relational & Vector Storage
    "psycopg[binary]>=3.1.19,<3.2.0",
    "pgvector>=0.3.1,<0.4.0",
    "sqlalchemy>=2.0.30,<2.1.0",
    "alembic>=1.13.1,<1.14.0",

    # Embedded Keyword Search
    "tantivy>=0.22.0,<0.23.0",

    # Caching & Job Queue (Redis Streams)
    "redis>=5.0.4,<5.1.0",

    # Code Parsing & AST (Bundled, zero-compile)
    "tree-sitter-languages>=1.10.2",

    # AI / Embeddings
    "sentence-transformers>=3.0.0,<3.1.0",
    "torch>=2.3.0,<2.4.0",

    # Utilities & Monitoring
    "prometheus-client>=0.20.0,<0.21.0",
    "httpx>=0.27.0,<0.28.0",
    "structlog>=24.1.0,<24.2.0"
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2.0",
    "pytest-asyncio>=0.23.7",
    "pytest-cov>=5.0.0",
    "ruff>=0.4.8",
    "mypy>=1.10.0"
]
```

---

## Scalability & Performance

### Scaling Stages:

1. **100 Users → 10K Files (Local / Small Team)**:
   - Single container / host.
   - SQLite or single PostgreSQL + pgvector instance.
   - Embedded Tantivy index stored on local disk/SSD.
   - `asyncio.Queue` for in-process asynchronous indexing.
   - Memory footprint: <1.5GB RAM.

2. **10K Users → 1M Files (Growing Organization)**:
   - FastAPI API server replicas (stateless).
   - PostgreSQL 16 with pgvector (HNSW indexes with `m=16, ef_construction=64`).
   - Redis cluster for response caching and Redis Streams job queue.
   - Dedicated indexing worker pool scaling horizontally based on Redis Stream consumer backlog (`XPENDING`).
   - Tantivy indices per repository on shared high-speed NVMe mounts.

3. **100K Users → 10M+ Files (Enterprise)**:
   - Repository-based index partitioning.
   - PostgreSQL read replicas with connection pooling (PgBouncer).
   - Redis Streams consumer groups partitioned by repository shard key.
   - Autoscaled embedding workers utilizing GPU/CPU inference batches.

### Latency Budget (Target: <150ms p95):
- **Query Embedding (`all-MiniLM-L6-v2`)**: ~5ms
- **Tantivy BM25 Keyword Search**: ~8ms
- **pgvector HNSW Vector Search**: ~15ms
- **Call Graph & Metadata Lookup**: ~10ms
- **Ranking & Serialization**: ~5ms
- **Redis Cache Hit Latency**: <5ms

---

## Reliability & Fault Tolerance

- **Worker Failures**: Redis Streams tracking via consumer groups (`XACK`, `XAUTOCLAIM` for abandoned indexing jobs).
- **Backpressure Protection**: Immediate HTTP 429 rejection on queue depth > 1000 to prevent OOM panics.
- **Index Corruption Protection**: Tantivy indices can be completely regenerated on demand directly from PostgreSQL symbol records.
- **Database Failover**: Read-replica routing for search queries if primary PostgreSQL node is undergoing maintenance.
- **Embedding Cache**: Cached symbol embeddings in PostgreSQL prevent redundant re-embedding during incremental file updates.
- **Graceful Fallbacks**: If vector search encounters unexpected latency, fallback directly to Tantivy BM25 + Symbol table lookups.

---

## Security & Isolation

- **Authentication**: JWT access tokens for IDE plugins and web dashboard; scoped API keys for CI/CD and AI agents.
- **Access Isolation**: Row-Level Security (RLS) or tenant filtering (`team_id`, `repo_id`) enforced on all database and Tantivy queries.
- **PII / Secret Scrubbing**: Automatic regex detection for API keys, passwords, and PII before indexing code tokens.
- **Transport & Storage Security**: TLS 1.3 in transit, AES-256 at rest for PostgreSQL storage volumes.

---

## Deployment Configuration

### Local / Single-Node (`docker-compose.yml`):

```yaml
version: '3.8'

services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: codepulse
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgrespassword
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redisdata:/data

  api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql+psycopg://postgres:postgrespassword@postgres:5432/codepulse
      REDIS_URL: redis://redis:6379/0
      TANTIVY_INDEX_PATH: /app/data/tantivy_indexes
    volumes:
      - tantivy_data:/app/data/tantivy_indexes
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started

  worker:
    build: .
    command: python -m app.workers.indexing_worker
    environment:
      DATABASE_URL: postgresql+psycopg://postgres:postgrespassword@postgres:5432/codepulse
      REDIS_URL: redis://redis:6379/0
      TANTIVY_INDEX_PATH: /app/data/tantivy_indexes
    volumes:
      - tantivy_data:/app/data/tantivy_indexes
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started

volumes:
  pgdata:
  redisdata:
  tantivy_data:
```

### Production Kubernetes Worker Deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: codepulse-indexing-worker
spec:
  replicas: 4
  selector:
    matchLabels:
      app: codepulse-worker
  template:
    metadata:
      labels:
        app: codepulse-worker
    spec:
      containers:
      - name: worker
        image: codepulse:latest
        command: ["python", "-m", "app.workers.indexing_worker"]
        env:
        - name: REDIS_STREAM_KEY
          value: "codepulse:indexing:jobs"
        - name: REDIS_CONSUMER_GROUP
          value: "indexing-workers"
        - name: REDIS_URL
          valueFrom:
            secretKeyRef:
              name: codepulse-secrets
              key: redis-url
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: codepulse-secrets
              key: database-url
        resources:
          requests:
            memory: "2Gi"
            cpu: "1000m"
          limits:
            memory: "4Gi"
            cpu: "2000m"
```

---

## Revised Phased Roadmap (12 Weeks)

- **Week 1-2: Phase 1 Core Parsers & Phased Graph Logic**
  - Modular parser architecture (`LanguageParser` ABC)
  - Implement `PythonParser` and `JavaScriptParser` (Day 1 scope)
  - **Phase 2A**: Basic import resolution for Python
  - **Phase 2B**: Call graph scoped to acyclic callables
- **Week 3: Phase 3A Embedding Benchmark & Pipeline**
  - Run **Phase 3A Embedding Benchmark Suite** (MiniLM vs CodeBERT on 1,000 real code search queries)
  - Select model based on accuracy vs latency metrics
  - Implement smart token-chunking pipeline
- **Week 4: Search Engine Core**
  - Setup embedded **Tantivy** BM25 index with identifier tokenizer
  - Setup **PostgreSQL + pgvector** with HNSW indexing
  - Implement and benchmark hybrid ranking logic (RRF / weighted merge)
- **Week 5: API Gateway & Backpressure Control**
  - Core search & symbol endpoints
  - JWT auth & API key validation
  - **Token Bucket Rate Limiting** (100 req/min search, 10 index/min, 50 graph/min)
  - **Backpressure Protection**: HTTP 429 rejection on queue depth > 1000
- **Week 6: Caching & Query Optimization**
  - Redis cache for frequent search queries
  - Cache invalidation on repo update
- **Week 7: Phase 6 Incremental Indexing Pipeline**
  - `asyncio.Queue` for local single-process MVP
  - **Redis Streams** producer/consumer workers for distributed scale
  - **Git Diff-Based Incremental Indexing** (symbol comparison, stale vector pruning, call graph edge update)
- **Week 8: Advanced Language Support & Impact Analysis**
  - Phase 5 expansion: Add **Go + Rust** parser plugins
  - Scoped impact analysis & symbol autocomplete
- **Week 9: Client Integrations**
  - VS Code extension MVP
  - Web search & exploration UI
- **Week 10: Performance & Load Testing**
  - Benchmark on repositories with 100K+ files
  - Profile memory, vector query latency (HNSW tuning), and Tantivy write speeds
- **Week 11: Testing & Reliability**
  - Unit tests (85%+ coverage)
  - Integration tests for end-to-end index and search flows
  - Worker crash & recovery tests (`XAUTOCLAIM`)
- **Week 12: Packaging & Deployment**
  - Production Docker Compose and Kubernetes manifests
  - `pyproject.toml` release package configuration
  - Documentation and API reference