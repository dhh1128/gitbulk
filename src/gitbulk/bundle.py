"""Build the single-file ``gitbulk`` zipapp (this.i nodes ``zpapb4n7``,
``pyvnd6kz``, ``vsrc4pn3``).

A stdlib :mod:`zipapp` archive that runs on any POSIX box with Python
3.10+. Two things distinguish it from a plain ``zipapp`` of the package:

* the version is **baked** into ``gitbulk/__init__.py`` at build time,
  because a zipapp has no ``.dist-info`` for ``importlib.metadata`` to read
  at runtime (node ``vsrc4pn3``); and
* PyYAML — gitbulk's only runtime third-party dependency — is **vendored**
  pure-Python by copying the installed ``yaml`` package and dropping the
  libyaml ``*.so`` C extension (node ``pyvnd6kz``). ``yaml.safe_load`` (all
  gitbulk uses) works on the pure-Python ``SafeLoader``.
"""

from __future__ import annotations

import shutil
import stat
import tempfile
import zipapp
from importlib import metadata
from pathlib import Path

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")

_MAIN = (
    "import sys\n"
    "from gitbulk.cli import main\n\n"
    "if __name__ == '__main__':\n"
    "    sys.exit(main())\n"
)


def _vendor_yaml(build_root: Path) -> None:
    """Copy the installed pure-Python ``yaml`` package into the build root.

    The libyaml C accelerator (``_yaml``) is a top-level extension module
    that lives *beside* the ``yaml`` package, so copying only the package
    directory already excludes it; we additionally strip any stray ``*.so``
    inside the tree as defense-in-depth. PyYAML's ``__init__`` imports the C
    loader inside a ``try/except ImportError`` and falls back to the
    pure-Python loaders, so the absence of ``_yaml`` is handled gracefully.
    """
    import yaml

    yaml_src = Path(yaml.__file__).resolve().parent
    yaml_dst = build_root / "yaml"
    shutil.copytree(yaml_src, yaml_dst, ignore=_IGNORE)
    for stray in yaml_dst.rglob("*.so"):
        stray.unlink()


def build_single_file(
    output: Path | str,
    source_root: Path | str | None = None,
    *,
    version: str | None = None,
) -> Path:
    """Build the ``gitbulk`` zipapp at ``output`` and return its path."""
    output = Path(output).resolve()
    if source_root is not None:
        package_src = Path(source_root) / "src" / "gitbulk"
    else:
        package_src = Path(__file__).resolve().parent
    if not package_src.exists():
        raise RuntimeError(f"package source not found: {package_src}")

    bundled_version = version if version is not None else metadata.version("gitbulk")

    with tempfile.TemporaryDirectory(prefix="gitbulk-bundle-") as temp_dir:
        build_root = Path(temp_dir)
        shutil.copytree(package_src, build_root / "gitbulk", ignore=_IGNORE)
        # Bake the version; the zipapp has no .dist-info at runtime.
        (build_root / "gitbulk" / "__init__.py").write_text(
            f'"""gitbulk package."""\n\n__version__ = "{bundled_version}"\n',
            encoding="utf-8",
        )
        _vendor_yaml(build_root)
        (build_root / "__main__.py").write_text(_MAIN, encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(
            build_root, target=output, interpreter="/usr/bin/env python3"
        )
    output.chmod(output.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return output
