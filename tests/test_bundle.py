"""Single-file zipapp bundle builder (this.i nodes ``zpapb4n7``, ``pyvnd6kz``).

The bundle is a stdlib ``zipapp`` named ``gitbulk`` with the version baked
in (no ``.dist-info`` at runtime) and PyYAML vendored pure-Python (the
libyaml ``*.so`` C extension is dropped). All tests run offline.
"""

import subprocess
import sys
import zipfile

import pytest

import gitbulk
from gitbulk.bundle import build_single_file


@pytest.fixture
def bundle(tmp_path):
    out = tmp_path / "gitbulk"
    build_single_file(out, version="7.7.7")
    return out


def test_build_creates_executable_file_with_env_shebang(bundle):
    assert bundle.exists()
    assert bundle.stat().st_mode & 0o111, "bundle should be executable"
    with bundle.open("rb") as fh:
        first_line = fh.readline()
    assert first_line == b"#!/usr/bin/env python3\n"


def test_bundle_is_a_zipapp_containing_the_package_and_entrypoint(bundle):
    names = zipfile.ZipFile(bundle).namelist()
    assert "__main__.py" in names
    assert "gitbulk/cli.py" in names
    assert "gitbulk/__init__.py" in names


def test_bundle_bakes_the_version_into_init(bundle):
    init = zipfile.ZipFile(bundle).read("gitbulk/__init__.py").decode("utf-8")
    assert '__version__ = "7.7.7"' in init


def test_bundle_vendors_pure_python_yaml_without_c_extension(bundle):
    names = zipfile.ZipFile(bundle).namelist()
    assert "yaml/__init__.py" in names, "PyYAML must be vendored"
    assert not [n for n in names if n.endswith(".so")], "C extensions must be dropped"
    assert not [n for n in names if "__pycache__" in n], "no bytecode caches"


def test_bundle_excludes_pyc_and_pycache_from_package(bundle):
    names = zipfile.ZipFile(bundle).namelist()
    assert not [n for n in names if n.endswith(".pyc")]


def test_default_version_comes_from_installed_metadata(tmp_path):
    out = tmp_path / "gitbulk"
    build_single_file(out)
    init = zipfile.ZipFile(out).read("gitbulk/__init__.py").decode("utf-8")
    assert f'__version__ = "{gitbulk.__version__}"' in init


def test_missing_package_source_raises(tmp_path):
    with pytest.raises(RuntimeError, match="package source not found"):
        build_single_file(tmp_path / "gitbulk", source_root=tmp_path / "nope")


def test_bundle_boots_and_reports_baked_version(bundle):
    """The zipapp runs end-to-end offline and prints its baked version."""
    result = subprocess.run(
        [sys.executable, str(bundle), "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "7.7.7" in result.stdout


def test_vendored_yaml_is_self_sufficient_without_site_packages(bundle):
    """With site-packages disabled (-S), the only importable yaml is the
    vendored one inside the zip; safe_load must still work."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import yaml; print(yaml.safe_load('k: v'))"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", code, str(bundle)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "{'k': 'v'}" in result.stdout
