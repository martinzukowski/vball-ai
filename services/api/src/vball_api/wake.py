"""Start the on-demand worker after queuing a job.

Deployed, the worker is a GitHub Actions workflow (.github/workflows/worker.yml) that drains the
queue and exits; the API starts it with a ``repository_dispatch`` event. Locally (no token set)
this is a no-op: the Docker worker polls the queue itself. Failures are logged, never raised:
the job is safely queued and the workflow's periodic run picks it up.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

import httpx
from fastapi import Depends, Request

from vball_core.logging import get_logger
from vball_core.settings import Settings

log = get_logger("vball_api.wake")


async def wake_worker(settings: Settings, client: httpx.AsyncClient | None = None) -> bool:
    if not settings.worker_dispatch_repo or settings.worker_dispatch_token is None:
        return False
    url = f"https://api.github.com/repos/{settings.worker_dispatch_repo}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.worker_dispatch_token.get_secret_value()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) if client is None else _borrow(client) as c:
            r = await c.post(url, headers=headers, json={"event_type": "process"})
        if r.status_code != 204:
            log.warning("worker_wake_rejected", status=r.status_code)
            return False
    except httpx.HTTPError as exc:
        log.warning("worker_wake_failed", error=type(exc).__name__)
        return False
    return True


class _borrow:
    """Use an injected client (tests) without closing it."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self.client

    async def __aexit__(self, *exc: object) -> None:
        return None


def get_waker(request: Request) -> Callable[[], Awaitable[bool]]:
    settings: Settings = request.app.state.settings

    async def wake() -> bool:
        return await wake_worker(settings)

    return wake


WakeWorker = Annotated[Callable[[], Awaitable[bool]], Depends(get_waker)]
