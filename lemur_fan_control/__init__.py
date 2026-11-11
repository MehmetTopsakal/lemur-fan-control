"""Lemur Pro fan control: installed commands and package information."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('lemur-fan-control')
except PackageNotFoundError:
    __version__ = '0.1.0'
