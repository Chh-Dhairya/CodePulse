"""
Phase 6.1 -- Full CLI command set (roadmap §37 item 6.1; §22 CLI
Strategy; §45 backlog B21: "All §22 commands implemented with --json
output").

Implemented under the user's explicit continuous-execution
authorization (see CODEPULSE_PROGRESS.md's standing-deviation notice).

Every new command follows the exact same established pattern `search`/
`impact` already used: construct an `IndexingEngine`, call
`index_directory(path)` (idempotent, cheap for an unchanged repo per
Phase 3's own incremental machinery), then delegate to the SAME shared
`IndexingEngine.analyze_*`/`find_similar_symbols` methods REST/MCP
already call -- no business logic is reimplemented in the CLI layer.
These tests call each `cmd_*` function directly with a constructed
`argparse.Namespace` (the smallest, most direct way to test CLI
command bodies without spawning a subprocess or driving argparse
itself, which is already exercised implicitly by `main()`'s own
subparser wiring) and capture `--json` output via `capsys`.

`index`/`update` DB-mutating tests, `symbol`/`refs`/`callers`/
`callees`/`similar`/`status` read-path tests, `doctor` diagnostics, and
`init`'s file-generation are all covered. `serve` (both the REST and
`--mcp` branches) is NOT covered here -- starting a real, long-running
server process is not practically unit-testable the way every other
command is; `cmd_serve`'s `--mcp` branch is a thin, already-tested
wrapper over `build_mcp_server` (Phase 5.3's own `test_mcp_server.py`
tests that function directly), and its REST branch is a thin wrapper
over `uvicorn.run` -- disclosed as a known, deliberate test gap in
CODEPULSE_PROGRESS.md's Phase 6.1 entry, not silently skipped.
`resolve_mcp_api_key` (added later, for the VS Code extension's Phase 3)
was extracted out of that untestable branch specifically because IT is
a small pure function, and is covered in its own section near the
bottom of this file.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import types

import pytest

import codepulse.cli as cli
from codepulse.config import reset_settings


@pytest.fixture(autouse=True)
def cleanup_settings():
    reset_settings()
    yield
    reset_settings()


def _git(repo_path, *args) -> None:
    subprocess.run(["git", *args], cwd=str(repo_path), check=True, capture_output=True, text=True)


def _init_repo(repo_path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", "-q")
    _git(repo_path, "config", "user.email", "test@codepulse.local")
    _git(repo_path, "config", "user.name", "CodePulse Test")


def _commit_all(repo_path, message: str) -> None:
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", message)


def _write(repo_path, rel_path: str, content: str) -> None:
    full = repo_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _db_url(tmp_path, name: str) -> str:
    db_path = tmp_path / name
    return f"sqlite:///{str(db_path).replace(os.sep, '/')}"


MOD_SOURCE = (
    'def helper():\n    """A helper."""\n    return 1\n\n\n'
    'def caller():\n    """Calls helper."""\n    return helper()\n'
)


def _make_repo(tmp_path, dirname):
    repo = tmp_path / dirname
    _init_repo(repo)
    _write(repo, "mod.py", MOD_SOURCE)
    _commit_all(repo, "initial")
    return repo


def _set_db(tmp_path, name):
    from codepulse.config import get_settings
    get_settings(reload=True, cli_overrides={"db_url": _db_url(tmp_path, name)})


def _read_json(capsys):
    out = capsys.readouterr().out
    return json.loads(out)


# --- init -----------------------------------------------------------------

def test_cmd_init_creates_a_config_file(tmp_path):
    target = tmp_path / "new_project"
    cli.cmd_init(argparse.Namespace(path=str(target), force=False))
    config_path = target / "codepulse.toml"
    assert config_path.exists()
    content = config_path.read_text(encoding="utf-8")
    assert "db_url" in content
    assert "embedding_model" in content


def test_cmd_init_refuses_to_overwrite_without_force(tmp_path, capsys):
    target = tmp_path / "existing_project"
    target.mkdir()
    (target / "codepulse.toml").write_text("db_url = \"sentinel\"\n", encoding="utf-8")

    cli.cmd_init(argparse.Namespace(path=str(target), force=False))
    assert "sentinel" in (target / "codepulse.toml").read_text(encoding="utf-8")

    cli.cmd_init(argparse.Namespace(path=str(target), force=True))
    assert "sentinel" not in (target / "codepulse.toml").read_text(encoding="utf-8")


# --- index / update ---------------------------------------------------------

def test_cmd_index_and_update_produce_valid_json(tmp_path, capsys):
    _set_db(tmp_path, "cli_index.db")
    repo = _make_repo(tmp_path, "repo_index")

    cli.cmd_index(argparse.Namespace(path=str(repo), json=True))
    body = _read_json(capsys)
    assert body["total_files"] == 1
    assert "repository_id" in body

    cli.cmd_update(argparse.Namespace(path=str(repo), json=True))
    body2 = _read_json(capsys)
    assert body2["repository_id"] == body["repository_id"]


# --- symbol / refs / callers / callees / similar / status / doctor ---------

def test_cmd_symbol_finds_and_reports_a_real_symbol(tmp_path, capsys):
    _set_db(tmp_path, "cli_symbol.db")
    repo = _make_repo(tmp_path, "repo_symbol")
    cli.cmd_symbol(argparse.Namespace(path=str(repo), symbol="helper", json=True))
    body = _read_json(capsys)
    assert body["matches"][0]["name"] == "helper"


def test_cmd_symbol_reports_not_found_for_a_missing_symbol(tmp_path, capsys):
    _set_db(tmp_path, "cli_symbol_404.db")
    repo = _make_repo(tmp_path, "repo_symbol_404")
    cli.cmd_symbol(argparse.Namespace(path=str(repo), symbol="does_not_exist", json=True))
    body = _read_json(capsys)
    assert body["error"] == "symbol_not_found"


def test_cmd_refs_shows_the_direct_caller(tmp_path, capsys):
    _set_db(tmp_path, "cli_refs.db")
    repo = _make_repo(tmp_path, "repo_refs")
    cli.cmd_refs(argparse.Namespace(path=str(repo), symbol="helper", json=True))
    body = _read_json(capsys)
    assert [c["symbol"] for c in body["callers"]] == ["caller"]


def test_cmd_callers_and_callees_match_analyze_impact(tmp_path, capsys):
    _set_db(tmp_path, "cli_callers.db")
    repo = _make_repo(tmp_path, "repo_callers")

    cli.cmd_callers(argparse.Namespace(path=str(repo), symbol="helper", depth=3, json=True))
    callers_body = _read_json(capsys)
    assert [c["symbol"] for c in callers_body["callers"]] == ["caller"]

    cli.cmd_callees(argparse.Namespace(path=str(repo), symbol="caller", depth=3, json=True))
    callees_body = _read_json(capsys)
    assert [c["symbol"] for c in callees_body["callees"]] == ["helper"]


def test_cmd_similar_returns_symbols_above_threshold(tmp_path, capsys):
    _set_db(tmp_path, "cli_similar.db")
    repo = _make_repo(tmp_path, "repo_similar")
    cli.cmd_similar(argparse.Namespace(path=str(repo), symbol="helper", threshold=0.0, limit=5, json=True))
    body = _read_json(capsys)
    assert "similar" in body


def test_cmd_status_reports_real_file_and_symbol_counts(tmp_path, capsys):
    _set_db(tmp_path, "cli_status.db")
    repo = _make_repo(tmp_path, "repo_status")
    cli.cmd_index(argparse.Namespace(path=str(repo), json=True))
    capsys.readouterr()  # discard index's own JSON output

    cli.cmd_status(argparse.Namespace(path=str(repo), json=True))
    body = _read_json(capsys)
    assert body["indexed"] is True
    assert body["file_count"] == 1
    assert body["symbol_count"] == 2
    assert body["last_snapshot_commit"] is not None


def test_cmd_status_reports_not_indexed_for_an_unindexed_path(tmp_path, capsys):
    _set_db(tmp_path, "cli_status_none.db")
    repo = tmp_path / "never_indexed"
    repo.mkdir()
    cli.cmd_status(argparse.Namespace(path=str(repo), json=True))
    body = _read_json(capsys)
    assert body["indexed"] is False


def test_cmd_modules_reports_clusters_and_rankings(tmp_path, capsys):
    """Backend-hardening addition (not a roadmap sprint item) -- CLI
    exposure of Phase 4.3's module-clustering engine capability
    (IndexingEngine.analyze_module_structure), matching the same
    thin-wrapper contract every other cmd_* command follows."""
    _set_db(tmp_path, "cli_modules.db")
    repo = _make_repo(tmp_path, "repo_modules")
    cli.cmd_modules(argparse.Namespace(path=str(repo), json=True))
    body = _read_json(capsys)
    assert body["repository_id"] > 0
    assert isinstance(body["clusters"], list)
    assert len(body["clusters"]) >= 1
    # mod.py is the repo's only file -- it must appear in exactly one cluster.
    all_files = [f for cluster in body["clusters"] for f in cluster["files"]]
    assert all_files == ["mod.py"]
    assert {row["file"] for row in body["ranked_by_inbound"]} == {"mod.py"}
    assert {row["file"] for row in body["ranked_by_outbound"]} == {"mod.py"}


def test_cmd_doctor_reports_all_checks_ok_in_a_healthy_environment(tmp_path, capsys):
    _set_db(tmp_path, "cli_doctor.db")
    repo = _make_repo(tmp_path, "repo_doctor")
    cli.cmd_doctor(argparse.Namespace(path=str(repo), json=True))
    body = _read_json(capsys)
    checks = {c["check"]: c["ok"] for c in body["checks"]}
    assert checks["database_reachable"] is True
    assert checks["embedding_model_loads"] is True
    assert checks["ignore_rules_active"] is True


# --- CLI-level repository isolation -----------------------------------------

def test_cli_commands_do_not_mix_up_two_repositories_sharing_the_same_database(tmp_path, capsys):
    """The CLI resolves a repository by its own path every time (not a
    passed-in id) -- but both repositories share the same underlying
    `db_url` in this test, exactly like a real multi-project machine
    using one shared local database would. A command run against repo
    A's path must never report repo B's symbols/counts."""
    _set_db(tmp_path, "cli_iso.db")
    repo_a = _make_repo(tmp_path, "repo_a_cli_iso")
    repo_b = tmp_path / "repo_b_cli_iso"
    _init_repo(repo_b)
    _write(repo_b, "mod.py", 'def only_in_b():\n    """Only in repo B."""\n    return 2\n')
    _commit_all(repo_b, "initial")

    cli.cmd_index(argparse.Namespace(path=str(repo_a), json=True))
    capsys.readouterr()
    cli.cmd_index(argparse.Namespace(path=str(repo_b), json=True))
    capsys.readouterr()

    cli.cmd_symbol(argparse.Namespace(path=str(repo_a), symbol="only_in_b", json=True))
    body = _read_json(capsys)
    assert body["error"] == "symbol_not_found", "repo A's own path must never resolve repo B's symbol"

    cli.cmd_status(argparse.Namespace(path=str(repo_a), json=True))
    status_a = _read_json(capsys)
    assert status_a["file_count"] == 1, "repo A's own status must reflect only its own one file"


# --- resolve_mcp_api_key (extracted from cmd_serve's --mcp branch) ----------
# Not a roadmap sprint item -- added for the VS Code extension's Phase 3
# (MCP integration): a process SPAWNING `codepulse serve --mcp` should be
# able to hand it the API key via an environment variable instead of a
# CLI argument, since CLI args are visible to other processes on the
# same machine. This one small pure function IS unit-testable, unlike
# the rest of `cmd_serve --mcp` (see this module's own docstring above).

def test_resolve_mcp_api_key_prefers_the_cli_value_when_both_are_given():
    assert cli.resolve_mcp_api_key("cli-key", {"CODEPULSE_MCP_API_KEY": "env-key"}) == "cli-key"


def test_resolve_mcp_api_key_falls_back_to_the_env_var():
    assert cli.resolve_mcp_api_key(None, {"CODEPULSE_MCP_API_KEY": "env-key"}) == "env-key"


def test_resolve_mcp_api_key_returns_none_when_neither_is_set():
    assert cli.resolve_mcp_api_key(None, {}) is None


def test_resolve_mcp_api_key_treats_an_empty_string_cli_value_as_unset():
    assert cli.resolve_mcp_api_key("", {"CODEPULSE_MCP_API_KEY": "env-key"}) == "env-key"


# --- main(): repository-path validation ------------------------------------
# Found by the 2026-10-02 audit: `callers /no/such/path foo` exited 0 with
# "0 found" and left an empty phantom repository (and, if needed, a new
# database file) behind. Exercised through main() (not cmd_* directly)
# because the check lives in main()'s dispatch.

@pytest.mark.parametrize("command", ["index", "update", "status", "modules"])
def test_main_rejects_a_nonexistent_repository_path_without_creating_a_database(
    tmp_path, monkeypatch, capsys, command
):
    db_file = tmp_path / "should_not_be_created.db"
    missing = tmp_path / "no_such_repo"
    monkeypatch.setattr(
        "sys.argv", ["codepulse", "--db-url", _db_url(tmp_path, db_file.name), command, str(missing)]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert not db_file.exists(), "validating the path must happen BEFORE any database is created"
    assert "not an existing directory" in capsys.readouterr().err


def test_main_symbol_style_command_with_json_reports_a_machine_readable_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["codepulse", "--db-url", _db_url(tmp_path, "x.db"), "callers", str(tmp_path / "nope"), "foo", "--json"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    body = json.loads(capsys.readouterr().out)
    assert body["error"] == "path_not_found"


def test_main_does_not_require_the_path_to_exist_for_init(tmp_path, monkeypatch):
    """`init` creates its target directory, so it must stay exempt."""
    target = tmp_path / "brand_new_dir"
    monkeypatch.setattr("sys.argv", ["codepulse", "init", str(target)])
    cli.main()
    assert (target / "codepulse.toml").exists()


# --- _display_db_path (main()'s "Using database: ..." diagnostic) ---------
# Not a roadmap sprint item -- db_url defaults to a CWD-relative
# "sqlite:///codepulse.db" with no prior feedback about which absolute
# file that actually is, so the same command run from two different
# directories silently uses two different databases. Confirmed directly
# with a real user hitting this twice in one session. This one small
# pure function (the rest of main()'s dispatch isn't unit-tested here,
# see this module's own docstring above) resolves a relative sqlite
# path to an absolute one purely for display.

def test_display_db_path_resolves_a_relative_sqlite_path_to_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shown = cli._display_db_path("sqlite:///codepulse.db")
    assert shown == f"sqlite:///{str(tmp_path / 'codepulse.db').replace(chr(92), '/')}"


def test_display_db_path_leaves_an_already_absolute_sqlite_path_unchanged(tmp_path):
    # An absolute path of THIS operating system: "C:/..." is absolute only on Windows (on Linux it is a relative path
    # that happens to start with "C:", which the function correctly resolves against the working directory; the
    # first CI run on GitHub's Linux machines caught the old test using a Windows path unconditionally).
    absolute = f"sqlite:///{(tmp_path / 'some' / 'abs' / 'path.db').as_posix()}"
    assert cli._display_db_path(absolute) == absolute


@pytest.mark.skipif(os.name != "nt", reason="a drive-letter path is absolute only on Windows")
def test_display_db_path_leaves_a_windows_drive_path_unchanged():
    assert cli._display_db_path("sqlite:///C:/some/abs/path.db") == "sqlite:///C:/some/abs/path.db"


def test_display_db_path_leaves_a_non_sqlite_url_unchanged():
    assert cli._display_db_path("postgresql://user@host/db") == "postgresql://user@host/db"


# --- `serve --mcp` loads the embedding model before serving stdio ------------
# Found by a live stdio-MCP check: the first semantic/hybrid `search_code`
# call imported sentence-transformers from the tool-call worker thread, which
# hung on Windows (>240s, vs 19s when the same import happens on the main
# thread). In-process MCP tests could never see this, so the regression
# guard is ordering: the model is loaded on the main thread BEFORE the stdio
# loop starts, and nothing but protocol messages is written to stdout.

class _FakeMCP:
    def __init__(self, events):
        self._events = events

    async def run_stdio_async(self):
        self._events.append("serve_stdio")


def _patch_serve_mcp(monkeypatch, events, preload_error=None):
    import codepulse.embeddings.embedder as embedder_module
    import codepulse.mcp_server as mcp_module

    class _FakeEmbedder:
        def preload(self):
            events.append("preload")
            if preload_error is not None:
                raise preload_error

    monkeypatch.setattr(embedder_module, "CodeEmbedder", _FakeEmbedder)
    monkeypatch.setattr(cli, "StorageManager", lambda db_url=None: object())
    monkeypatch.setattr(cli, "IndexingEngine", lambda storage=None, embedder=None: types.SimpleNamespace(embedder=embedder))
    monkeypatch.setattr(mcp_module, "build_mcp_server", lambda storage, engine, key: _FakeMCP(events))


def _serve_mcp_args():
    return argparse.Namespace(mcp=True, api_key="cpk_test", host="127.0.0.1", port=8000)


def test_serve_mcp_preloads_the_embedder_before_the_stdio_loop(monkeypatch):
    events: list[str] = []
    _patch_serve_mcp(monkeypatch, events)
    cli.cmd_serve(_serve_mcp_args())
    assert events == ["preload", "serve_stdio"]


def test_serve_mcp_still_starts_when_the_embedding_model_cannot_load(monkeypatch, capsys):
    from codepulse.embeddings.embedder import EmbeddingUnavailableError

    events: list[str] = []
    _patch_serve_mcp(monkeypatch, events, preload_error=EmbeddingUnavailableError("no model"))
    cli.cmd_serve(_serve_mcp_args())
    assert events == ["preload", "serve_stdio"]  # keyword search must still be served
    captured = capsys.readouterr()
    assert "no model" in captured.err  # the degradation is stated, not silent
    assert captured.out == ""  # stdout is the MCP protocol channel


def test_serve_mcp_writes_nothing_to_stdout(monkeypatch, capsys):
    events: list[str] = []
    _patch_serve_mcp(monkeypatch, events)
    cli.cmd_serve(_serve_mcp_args())
    assert capsys.readouterr().out == ""
