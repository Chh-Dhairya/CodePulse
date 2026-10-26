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


# --- publishable metadata (2026-10-15 publication audit, FUTURE_CHANGES item 23) ------------

def test_the_distribution_name_is_not_the_taken_pypi_name():
    """`codepulse` (and codepulse-cli / codepulse-ai) are taken on PyPI by unrelated projects; the import package
    and the console script stay `codepulse`, only the distribution name differs."""
    project = _pyproject()["project"]
    assert project["name"] == "codepulse-engine"
    assert "codepulse" in project.get("scripts", {})


def test_pyproject_carries_the_metadata_pypi_shows():
    project = _pyproject()["project"]
    assert len(project["description"]) >= 60, "a real one-sentence description"
    assert project.get("authors") and project["authors"][0].get("name")
    assert len(project.get("keywords", [])) >= 5
    classifiers = project.get("classifiers", [])
    assert "License :: OSI Approved :: MIT License" in classifiers
    assert "Programming Language :: Python :: 3.11" in classifiers
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert any(c.startswith("Development Status ::") for c in classifiers)
    assert any(c.startswith("Intended Audience ::") for c in classifiers)
    assert project.get("readme") == "README.md"


def test_no_placeholder_text_in_the_published_metadata():
    project = _pyproject()["project"]
    text = repr(project)
    for placeholder in ("YOUR-GITHUB", "TODO", "FIXME", "example.com", "<user>"):
        assert placeholder not in text, f"placeholder {placeholder!r} would be published to PyPI"


def test_pyproject_links_to_the_real_repository():
    """PyPI shows these in the sidebar; they must point at the real GitHub project, not a placeholder."""
    urls = _pyproject()["project"].get("urls", {})
    base = "https://github.com/Chh-Dhairya/CodePulse"
    assert urls.get("Homepage") == base
    assert urls.get("Repository") == base
    assert urls.get("Issues") == f"{base}/issues"
    assert urls.get("Documentation", "").startswith(f"{base}/")
    assert all(u.startswith("https://") for u in urls.values())


def test_the_readme_has_no_relative_links_because_pypi_does_not_resolve_them():
    """PyPI renders README.md as the project page and leaves a link like `docs/x.md` pointing nowhere."""
    import re

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    relative = [m for m in re.findall(r"\]\(([^)\s]+)\)", readme) if not re.match(r"(https?://|#|mailto:)", m)]
    assert relative == [], f"relative links break on the PyPI page: {relative}"


def test_build_output_is_ignored_so_it_cannot_be_committed():
    ignored = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert "dist/" in ignored and "build/" in ignored
