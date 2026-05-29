"""Version resolution (this.i node ``vsrc4pn3``).

pyproject.toml is the single source of version truth. ``__init__`` derives
``__version__`` from installed package metadata, falling back to a dev
sentinel when the package was never installed (a bare source tree). The
zipapp bundle bakes a literal in instead (it has no ``.dist-info``); that
path is covered by ``test_bundle``.
"""

import importlib.metadata

import gitbulk
from gitbulk import _DEV_VERSION, _resolve_version


def test_resolve_version_reads_package_metadata():
    """When metadata is present, the lookup result is returned verbatim."""
    assert _resolve_version(lambda name: "1.2.3") == "1.2.3"


def test_resolve_version_passes_the_distribution_name():
    """The resolver looks up the ``gitbulk`` distribution, not something else."""
    seen = []

    def _lookup(name: str) -> str:
        seen.append(name)
        return "9.9.9"

    assert _resolve_version(_lookup) == "9.9.9"
    assert seen == ["gitbulk"]


def test_resolve_version_falls_back_to_dev_sentinel_when_uninstalled():
    """A bare source tree has no metadata; the dev sentinel is returned."""

    def _raise(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    assert _resolve_version(_raise) == _DEV_VERSION


def test_module_version_matches_installed_metadata():
    """In the editable test install, ``__version__`` equals package metadata."""
    assert gitbulk.__version__ == importlib.metadata.version("gitbulk")
