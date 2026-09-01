from codepulse.models import SymbolType
from codepulse.parser.javascript import JavaScriptParser


def test_javascript_functions_and_arrows():
    parser = JavaScriptParser()
    code = '''
/**
 * Adds two numbers together.
 */
function add(a, b) {
    if (a < 0) return 0;
    return a + b;
}

const multiply = (x, y) => {
    return x * y;
};
'''
    symbols = parser.extract_symbols(code, file_path="math.js")
    assert len(symbols) == 2

    # 1. function add
    sym1 = symbols[0]
    assert sym1.name == "add"
    assert sym1.type == SymbolType.FUNCTION
    assert sym1.docstring == "Adds two numbers together."
    assert sym1.start_line >= 4
    assert sym1.complexity >= 2

    # 2. const multiply
    sym2 = symbols[1]
    assert sym2.name == "multiply"
    assert sym2.type == SymbolType.FUNCTION
    assert sym2.start_line >= 9


def test_javascript_classes_and_methods():
    parser = JavaScriptParser()
    code = '''
class AuthService {
    /**
     * Authenticates a user.
     */
    login(username, password) {
        return true;
    }
}
'''
    symbols = parser.extract_symbols(code, file_path="auth.js")
    assert len(symbols) == 2

    cls_sym = symbols[0]
    assert cls_sym.name == "AuthService"
    assert cls_sym.type == SymbolType.CLASS

    method_sym = symbols[1]
    assert method_sym.name == "login"
    assert method_sym.type == SymbolType.METHOD
    assert method_sym.parent_symbol == "AuthService"
    assert method_sym.docstring == "Authenticates a user."


def test_javascript_imports():
    parser = JavaScriptParser()
    code = '''
import React, { useState, useEffect as useEff } from 'react';
import * as utils from './utils';
const path = require('path');
'''
    imports = parser.extract_imports(code, file_path="index.js")
    assert len(imports) == 3

    # 1. import React, { useState, useEffect as useEff } from 'react'
    assert imports[0].module == "react"
    assert len(imports[0].items) >= 2
    item_names = {i.name: i.alias for i in imports[0].items}
    assert "React" in item_names
    assert "useState" in item_names

    # 2. import * as utils from './utils'
    assert imports[1].module == "./utils"
    assert imports[1].is_relative is True

    # 3. const path = require('path')
    assert imports[2].module == "path"


def test_javascript_calls():
    parser = JavaScriptParser()
    code = '''
function render() {
    logMessage("rendering");
    db.query("SELECT *");
}
'''
    symbols = parser.extract_symbols(code, file_path="app.js")
    calls = parser.extract_calls(code, file_path="app.js", symbols=symbols)

    assert len(calls) == 2
    assert calls[0].caller_symbol == "render"
    assert calls[0].callee_name == "logMessage"
    assert calls[1].caller_symbol == "render"
    assert calls[1].callee_name == "query"
