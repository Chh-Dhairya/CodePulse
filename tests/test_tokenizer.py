from codepulse.search.tokenizer import split_identifier, tokenize_code


def test_split_identifier():
    assert split_identifier("createDbConnection") == ["create", "Db", "Connection"]
    assert split_identifier("POSTGRES_POOL_SIZE") == ["POSTGRES", "POOL", "SIZE"]
    assert split_identifier("get_user_by_id") == ["get", "user", "by", "id"]
    assert split_identifier("ASTVisitorV2") == ["AST", "Visitor", "V2"]
    assert split_identifier("simple") == ["simple"]


def test_tokenize_code():
    code_text = "def createDbConnection(host_name: str, port_num: int = 5432):"
    tokens = tokenize_code(code_text)

    # Should contain full tokens
    assert "createdbconnection" in tokens
    # Should contain sub-tokens
    assert "create" in tokens
    assert "db" in tokens
    assert "connection" in tokens
    assert "host" in tokens
    assert "name" in tokens
    assert "port" in tokens
    assert "num" in tokens
