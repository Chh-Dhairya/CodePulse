# CodePulse ⚡

> Production-grade semantic codebase search engine with hybrid retrieval (BM25 + dense embeddings + symbol call graphs) and low-latency API for IDEs and AI agents.

## Architecture Highlights
- **AST Parsing**: Modular `tree-sitter-languages` support across languages (Python & JavaScript/TypeScript in Phase 1).
- **Search Engine**: Embedded Tantivy (Rust BM25) + PostgreSQL `pgvector` (HNSW) for hybrid retrieval.
- **Queue & Workers**: In-process `asyncio.Queue` for local runs, scalable to **Redis Streams** for distributed workers.
- **Incremental Indexing**: Git diff-based symbol change detection, surgical vector updates, and local call graph updates.

## Getting Started

### 1. Prerequisites
- Python 3.11+
- (Optional for full backend) Docker & Docker Compose for PostgreSQL + pgvector and Redis.

### 2. Installation
```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"
```

### 3. Running Tests
```bash
pytest tests/ -v
```
