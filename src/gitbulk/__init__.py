"""gitbulk package version resolution.

The single source of version truth is ``pyproject.toml`` (this.i node
``vsrc4pn3``). When gitbulk is installed (pip / pipx / editable),
``__version__`` comes from installed package metadata. When running from a
source tree that was never installed, metadata is absent and we fall back
to a dev sentinel. The zipapp bundle has no ``.dist-info`` either, so
``bundle.py`` bakes a literal ``__version__`` into this module at build
time, replacing this whole file.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _version

_DEV_VERSION = "0.0.0+dev"


def _resolve_version(lookup=_version) -> str:
    """Return the installed ``gitbulk`` version, or a dev sentinel.

    ``lookup`` is injectable so both branches are testable offline; in
    production it is :func:`importlib.metadata.version`.
    """
    try:
        return lookup("gitbulk")
    except PackageNotFoundError:
        return _DEV_VERSION


__version__ = _resolve_version()
