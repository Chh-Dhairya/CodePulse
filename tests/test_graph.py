from codepulse.parser.factory import get_parser_for_file
from codepulse.parser.graph import CallGraphBuilder, ImportResolver
from codepulse.parser.python import PythonParser


def test_import_resolution():
    py_parser = PythonParser()

    file1_code = '''
def hash_password(pw: str) -> str:
    return "hashed"
'''
    res1 = py_parser.parse_file("app/security/hashing.py", file1_code)

    file2_code = '''
from .hashing import hash_password
from ..config import DB_URI

def register_user(username, password):
    hashed = hash_password(password)
    return hashed
'''
    res2 = py_parser.parse_file("app/security/auth.py", file2_code)

    resolver = ImportResolver([res1, res2])

    # Resolving relative import: from .hashing import hash_password
    imp1 = res2.imports[0]
    resolved = resolver.resolve_import_path("app/security/auth.py", imp1)
    assert resolved == "app/security/hashing.py"


def test_call_graph_and_impact_analysis():
    py_parser = PythonParser()

    file_a = '''
def db_connect():
    return "conn"

def db_query(q):
    conn = db_connect()
    return conn
'''
    res_a = py_parser.parse_file("db.py", file_a)

    file_b = '''
from db import db_query

def get_user_profile(user_id):
    return db_query("SELECT *")

def render_dashboard(user_id):
    user = get_user_profile(user_id)
    return user
'''
    res_b = py_parser.parse_file("service.py", file_b)

    builder = CallGraphBuilder([res_a, res_b])

    # 1. Check downstream callees of render_dashboard
    callees = builder.get_callees("render_dashboard", "service.py", max_depth=3)
    callee_names = [c["symbol"] for c in callees]
    assert "get_user_profile" in callee_names
    assert "db_query" in callee_names
    assert "db_connect" in callee_names

    # 2. Check upstream callers (blast radius) of db_connect
    callers = builder.get_callers("db_connect", "db.py", max_depth=3)
    caller_names = [c["symbol"] for c in callers]
    assert "db_query" in caller_names
    assert "get_user_profile" in caller_names
    assert "render_dashboard" in caller_names


# --- Phase 3.4: CallEdge.confidence scoring (roadmap §37 item 3.4; §11) ----

def _edge(builder, source_symbol_name, target_symbol_name):
    matches = [
        e for e in builder.edges
        if e.source_symbol_name == source_symbol_name and e.target_symbol_name == target_symbol_name
    ]
    assert len(matches) == 1, (
        f"expected exactly one edge {source_symbol_name}->{target_symbol_name}, found {len(matches)}"
    )
    return matches[0]


def test_call_edge_confidence_high_for_import_resolved_call():
    """§11: "high -- resolved through an explicit import mapping to a
    known file+symbol." `db_query` is imported into service.py from a
    real db.py that genuinely defines it."""
    py_parser = PythonParser()
    res_a = py_parser.parse_file(
        "db.py",
        'def db_connect():\n    return "conn"\n\n\ndef db_query(q):\n    conn = db_connect()\n    return conn\n',
    )
    res_b = py_parser.parse_file(
        "service.py",
        'from db import db_query\n\n\ndef get_user_profile(user_id):\n    return db_query("SELECT *")\n',
    )
    builder = CallGraphBuilder([res_a, res_b])

    edge = _edge(builder, "get_user_profile", "db_query")
    assert edge.confidence == "high"
    assert edge.target_file == "db.py"


def test_call_edge_confidence_low_for_same_file_bare_name_match():
    """§11: "low -- resolved only by bare-name match against same-file
    symbols." `db_connect` is called from `db_query` within the SAME
    file -- exactly the ambiguity §3.5's audit finding names (a bare
    name match, not a verified cross-file resolution)."""
    py_parser = PythonParser()
    res_a = py_parser.parse_file(
        "db.py",
        'def db_connect():\n    return "conn"\n\n\ndef db_query(q):\n    conn = db_connect()\n    return conn\n',
    )
    builder = CallGraphBuilder([res_a])

    edge = _edge(builder, "db_query", "db_connect")
    assert edge.confidence == "low"
    assert edge.target_file == "db.py"


def test_call_edge_confidence_unresolved_for_a_true_external_call():
    """§11: "unresolved -- the callee couldn't be matched to any known
    symbol at all." `json.dumps` is neither a local symbol nor anything
    reachable via this file's own import map targets."""
    py_parser = PythonParser()
    res = py_parser.parse_file(
        "serializer.py",
        'import json\n\n\ndef to_json(data):\n    return json.dumps(data)\n',
    )
    builder = CallGraphBuilder([res])

    matches = [e for e in builder.edges if e.source_symbol_name == "to_json"]
    assert len(matches) == 1
    edge = matches[0]
    assert edge.confidence == "unresolved"
    assert edge.target_file is None


def test_call_edge_confidence_unresolved_when_import_resolves_to_a_file_but_not_a_known_symbol():
    """§11's "high" tier requires a known file AND a known symbol, not
    just a known file. `helper_that_does_not_exist` is imported from a
    real, resolvable db.py -- but db.py genuinely does not define any
    symbol by that name (e.g. it could be a re-exported alias or a
    parser gap) -- so this must NOT be silently upgraded to "high" just
    because the import path itself resolved."""
    py_parser = PythonParser()
    res_a = py_parser.parse_file("db.py", 'def db_connect():\n    return "conn"\n')
    res_b = py_parser.parse_file(
        "service.py",
        'from db import helper_that_does_not_exist\n\n\n'
        'def call_it():\n    return helper_that_does_not_exist()\n',
    )
    builder = CallGraphBuilder([res_a, res_b])

    edge = _edge(builder, "call_it", "helper_that_does_not_exist")
    assert edge.confidence == "unresolved"
    # The resolved target FILE is still recorded (real, useful
    # information) even though the exact symbol couldn't be confirmed.
    assert edge.target_file == "db.py"


def test_parser_factory():
    p_py = get_parser_for_file("main.py")
    assert p_py is not None
    assert p_py.language == "python"

    p_js = get_parser_for_file("app.jsx")
    assert p_js is not None
    assert p_js.language == "javascript"

    p_ts = get_parser_for_file("index.ts")
    assert p_ts is not None
    assert p_ts.language == "typescript"

    p_unknown = get_parser_for_file("doc.md")
    assert p_unknown is None
