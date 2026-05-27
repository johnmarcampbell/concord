"""Concord: collect, index, and search U.S. Congress data."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("congress-concord")
except PackageNotFoundError:  # pragma: no cover — only happens in odd dev setups
    # The package isn't installed (e.g. running from a source checkout without
    # `uv sync` having materialised the install metadata). Use a sentinel that
    # sorts below every real version so downstream version checks fail safely.
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
