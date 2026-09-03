"""Shared request-scoped dependencies for the API layer.

This module exists to break an import cycle: :mod:`app.api.auth` needs the
request ``meta`` (which needs the settings), and :mod:`app.api.dependencies`
needs the authenticated principal (which needs auth). Both of them need the
*same* settings dependency, so it lives here — a leaf module that imports
nothing from either.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings, get_settings


async def get_app_settings(request: Request) -> Settings:
    """Return the settings bound to *this* application.

    ``create_app`` stores its own :class:`Settings` on ``app.state``, and
    middleware reads the same attribute. Falling back to the process default
    would let a second app (or a test that built its own) be judged by settings
    it never asked for — which for ``enforce_api_keys`` is the difference
    between an authenticated and an anonymous request.
    """
    return getattr(request.app.state, "settings", None) or get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
