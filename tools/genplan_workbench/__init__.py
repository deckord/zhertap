"""Offline operator workbench for georeferencing scanned urban plans."""

from typing import Any

__all__ = ["create_app"]


def create_app(*args: Any, **kwargs: Any) -> Any:
    from .server import create_app as server_create_app

    return server_create_app(*args, **kwargs)
