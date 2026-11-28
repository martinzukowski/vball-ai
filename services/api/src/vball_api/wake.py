"""Start the on-demand worker after queuing a job.

Deployed, the workers run on Modal (infra/modal/vball_app.py): the API calls its ``wake``
endpoint. Without a wake URL it falls back to the GitHub Actions worker
(.github/workflows/worker.yml) via ``repository_dispatch``. Locally (nothing configured)
this is a no-op: the Docker worker polls the queue itself. Failures are logged, never raised:
the job is safely queued and the periodic check (Modal `sweep`) picks it up.
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
    if settings.worker_wake_url and settings.worker_wake_token is not None:
        # Modal (infra/modal/vball_app.py `wake`): starts the right worker and returns at once.
        url, ok = settings.worker_wake_url, 200
        headers = {"Authorization": f"Bearer {settings.worker_wake_token.get_secret_value()}"}
        body: dict[str, str] = {}
    elif settings.worker_dispatch_repo and settings.worker_dispatch_token is not None:
        url, ok = f"https://api.github.com/repos/{settings.worker_dispatch_repo}/dispatches", 204
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {settings.worker_dispatch_token.get_secret_value()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = {"event_type": "process"}
    else:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) if client is None else _borrow(client) as c:
            r = await c.post(url, headers=headers, json=body)
        if r.status_code != ok:
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
