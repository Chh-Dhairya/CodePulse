from codepulse.models import Symbol, SymbolType
from codepulse.search.bm25 import BM25Index
from codepulse.search.vector import VectorIndex
import numpy as np


def test_bm25_search():
    index = BM25Index()

    sym1 = Symbol(
        name="create_db_connection",
        type=SymbolType.FUNCTION,
        file_path="db/connection.py",
        start_line=1,
        end_line=10,
        signature="def create_db_connection(host, port):",
        docstring="Creates a PostgreSQL database connection with pooling.",
        body="return psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=10)",
        language="python",
    )

    sym2 = Symbol(
        name="authenticate_user",
        type=SymbolType.FUNCTION,
        file_path="auth/jwt.py",
        start_line=1,
        end_line=15,
        signature="def authenticate_user(username, password):",
        docstring="Validates user credentials against password hash.",
        body="return verify_password(password, user.hash)",
        language="python",
    )

    index.add_symbol("sym1", sym1)
    index.add_symbol("sym2", sym2)
    index.build()

    # Search for database connection
    results = index.search("database connection pooling")
    assert len(results) > 0
    assert results[0]["doc_id"] == "sym1"
    assert results[0]["normalized_score"] > 0.0

    # Search for auth
    results_auth = index.search("user password verification")
    assert len(results_auth) > 0
    assert results_auth[0]["doc_id"] == "sym2"


def test_vector_index_search():
    vec_index = VectorIndex(dimension=4)

    # 3 mock vectors
    v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    v3 = np.array([0.7, 0.7, 0.0, 0.0], dtype=np.float32)

    vec_index.add("doc1", v1, {"name": "doc1"})
    vec_index.add("doc2", v2, {"name": "doc2"})
    vec_index.add("doc3", v3, {"name": "doc3"})

    query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    results = vec_index.search(query, top_k=3)

    assert len(results) == 3
    # Top match should be doc1 (similarity 1.0)
    assert results[0]["doc_id"] == "doc1"
    assert round(results[0]["similarity"], 2) == 1.0
    # Second should be doc3
    assert results[1]["doc_id"] == "doc3"
