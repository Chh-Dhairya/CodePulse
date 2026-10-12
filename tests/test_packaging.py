"""
Packaging/publication-readiness guards (not a roadmap sprint item).

Found by the 2026-10-02 resumption audit: `pyproject.toml` declared no
`[project.scripts]`, so no `codepulse` executable existed and the VS Code
extension's default `codepulse.serverCommand` could never work out of
the box; the project also had no LICENSE file. These tests pin both so a
future edit can't silently regress them.
"""
from __future__ import annotations

import importlib
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_codepulse_console_script_is_declared_and_resolves_to_a_callable():
    scripts = _pyproject()["project"].get("scripts", {})
    assert "codepulse" in scripts, "no `codepulse` console script -- `pip install` would put no command on PATH"

    module_name, _, attr = scripts["codepulse"].partition(":")
    target = getattr(importlib.import_module(module_name), attr)
    assert callable(target), f"console script target {scripts['codepulse']!r} is not callable"


def test_project_declares_a_license_and_ships_the_license_file():
    project = _pyproject()["project"]
    assert project.get("license"), "pyproject.toml declares no license"
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text and "Permission is hereby granted" in license_text


def test_gitattributes_normalizes_line_endings():
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "text=auto" in attrs
