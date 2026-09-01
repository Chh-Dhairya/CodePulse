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
