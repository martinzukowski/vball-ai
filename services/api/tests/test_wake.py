"""The API starts the GitHub Actions worker after queuing work, and never fails a request on it."""

from __future__ import annotations

import httpx

from vball_api.wake import wake_worker
from vball_core.settings import Settings


def settings(**kw: object) -> Settings:
    return Settings(database_url="postgresql+psycopg://u:p@localhost/db", **kw)  # type: ignore[arg-type]


def client(status: int, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_noop_when_not_configured() -> None:
    seen: list[httpx.Request] = []
    assert await wake_worker(settings(), client(204, seen)) is False
    assert seen == []


async def test_sends_repository_dispatch() -> None:
    seen: list[httpx.Request] = []
    s = settings(worker_dispatch_repo="me/repo", worker_dispatch_token="tok")
    assert await wake_worker(s, client(204, seen)) is True
    (req,) = seen
    assert str(req.url) == "https://api.github.com/repos/me/repo/dispatches"
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.content == b'{"event_type":"process"}'


async def test_rejection_is_not_an_error() -> None:
    s = settings(worker_dispatch_repo="me/repo", worker_dispatch_token="bad")
    assert await wake_worker(s, client(401, [])) is False


async def test_network_failure_is_not_an_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    s = settings(worker_dispatch_repo="me/repo", worker_dispatch_token="tok")
    c = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    assert await wake_worker(s, c) is False


async def test_modal_wake_takes_precedence() -> None:
    seen: list[httpx.Request] = []
    s = settings(
        worker_wake_url="https://me--vball-wake.modal.run",
        worker_wake_token="secret",
        worker_dispatch_repo="me/repo",
        worker_dispatch_token="tok",
    )
    assert await wake_worker(s, client(200, seen)) is True
    (req,) = seen
    assert str(req.url) == "https://me--vball-wake.modal.run"
    assert req.headers["Authorization"] == "Bearer secret"


async def test_modal_wake_rejected_is_not_an_error() -> None:
    s = settings(worker_wake_url="https://me--vball-wake.modal.run", worker_wake_token="bad")
    assert await wake_worker(s, client(401, [])) is False
