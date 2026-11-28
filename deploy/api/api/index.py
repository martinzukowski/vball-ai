"""Vercel entry point for the VBall API (project root: deploy/api).

The build step copies vball_core and vball_api into ./_src (see vercel.json). Vercel's Python
runtime may not run ASGI lifespan events, so startup (database engine, JWKS cache, storage
client) runs on the first request instead, once per warm instance.
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_src"))

from vball_api.main import create_app

fastapi_app = create_app()
_stack = AsyncExitStack()
_started = False


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    global _started
    if scope["type"] == "lifespan":  # the runtime does run lifespan: let FastAPI handle it
        _started = True
        await fastapi_app(scope, receive, send)
        return
    if not _started:
        await _stack.enter_async_context(fastapi_app.router.lifespan_context(fastapi_app))
        _started = True
    await fastapi_app(scope, receive, send)
