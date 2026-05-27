"""Module-level registry of Invariant subclasses.

See this.i node ``ivp4wq7n``. Tests should call ``_clear()`` in a fixture
to avoid registration leaking across tests.
"""

from __future__ import annotations

from typing import TypeVar

from gitbulk.invariants.base import Invariant

_T = TypeVar("_T", bound=type[Invariant])

_REGISTRY: dict[str, type[Invariant]] = {}


def register(cls: _T) -> _T:
    """Decorator. Registers an Invariant subclass keyed by its ``name``."""
    if cls.name in _REGISTRY:
        raise ValueError(f"duplicate invariant name: {cls.name!r}")
    _REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[Invariant]:
    """Return the registered Invariant class with this ``name``. KeyError if absent."""
    return _REGISTRY[name]


def all_invariants() -> dict[str, type[Invariant]]:
    """Shallow copy of the registry."""
    return dict(_REGISTRY)


def for_subcommand(subcommand: str) -> list[type[Invariant]]:
    """All registered invariants that declare ``subcommand`` in their applies-to set."""
    return [c for c in _REGISTRY.values() if subcommand in c.subcommands]


def _clear() -> None:
    """Test-only: empty the registry."""
    _REGISTRY.clear()
