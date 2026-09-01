from codepulse.models import SymbolType
from codepulse.parser.python import PythonParser


def test_python_function_extraction():
    parser = PythonParser()
    code = '''
def calculate_metrics(data: list[int], factor: float = 1.0) -> float:
    """Calculate normalized sum with scaling factor."""
    if not data:
        return 0.0
    total = sum(data) * factor
    return total

async def fetch_user(user_id: int):
    """Fetch user by identifier."""
    pass
'''
    symbols = parser.extract_symbols(code, file_path="app/metrics.py")
    assert len(symbols) == 2

    # Verify calculate_metrics
    func1 = symbols[0]
    assert func1.name == "calculate_metrics"
    assert func1.type == SymbolType.FUNCTION
    assert func1.file_path == "app/metrics.py"
    assert func1.start_line == 2
    assert func1.end_line == 7
    assert "def calculate_metrics" in func1.signature
    assert func1.docstring == "Calculate normalized sum with scaling factor."
    assert func1.complexity >= 2
    assert func1.body_hash != ""

    # Verify fetch_user
    func2 = symbols[1]
    assert func2.name == "fetch_user"
    assert func2.type == SymbolType.FUNCTION
    assert func2.docstring == "Fetch user by identifier."


def test_python_class_and_methods():
    parser = PythonParser()
    code = '''
class ConnectionPool:
    """Manages database connections."""
    
    def __init__(self, host: str, port: int):
        """Initialize pool."""
        self.host = host
        self.port = port
        
    def acquire(self):
        return "conn"
'''
    symbols = parser.extract_symbols(code, file_path="db/pool.py")
    assert len(symbols) == 3

    cls_sym = symbols[0]
    assert cls_sym.name == "ConnectionPool"
    assert cls_sym.type == SymbolType.CLASS
    assert cls_sym.docstring == "Manages database connections."

    init_sym = symbols[1]
    assert init_sym.name == "__init__"
    assert init_sym.type == SymbolType.METHOD
    assert init_sym.parent_symbol == "ConnectionPool"
    assert init_sym.docstring == "Initialize pool."

    acq_sym = symbols[2]
    assert acq_sym.name == "acquire"
    assert acq_sym.type == SymbolType.METHOD
    assert acq_sym.parent_symbol == "ConnectionPool"


def test_python_imports():
    parser = PythonParser()
    code = '''
import os
import sys as system
from typing import Optional, List as Lst
from .utils import format_string
from ..database.models import User
'''
    imports = parser.extract_imports(code, file_path="app/service.py")
    assert len(imports) == 5

    # 1. import os
    assert imports[0].module == "os"
    assert not imports[0].is_relative

    # 2. import sys as system
    assert imports[1].module == "sys"
    assert imports[1].items[0].alias == "system"

    # 3. from typing import Optional, List as Lst
    assert imports[2].module == "typing"
    assert len(imports[2].items) == 2
    assert imports[2].items[0].name == "Optional"
    assert imports[2].items[1].name == "List"
    assert imports[2].items[1].alias == "Lst"

    # 4. from .utils import format_string
    assert imports[3].module == "utils"
    assert imports[3].is_relative is True
    assert imports[3].level == 1
    assert imports[3].items[0].name == "format_string"

    # 5. from ..database.models import User
    assert imports[4].module == "database.models"
    assert imports[4].is_relative is True
    assert imports[4].level == 2
    assert imports[4].items[0].name == "User"


def test_python_call_extraction():
    parser = PythonParser()
    code = '''
def helper():
    return 42

def process():
    val = helper()
    conn.execute("SELECT 1")
    return val
'''
    symbols = parser.extract_symbols(code, file_path="main.py")
    calls = parser.extract_calls(code, file_path="main.py", symbols=symbols)

    assert len(calls) == 2
    assert calls[0].caller_symbol == "process"
    assert calls[0].callee_name == "helper"

    assert calls[1].caller_symbol == "process"
    assert calls[1].callee_name == "execute"
