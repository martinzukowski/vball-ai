"""Rally-analysis embeddings split across helper machines join to the one-pass result, and a
missing helper never changes the result (its slice is redone locally)."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vball_core.settings import Settings
from vball_worker import shards


def rows(start: int, n: int) -> np.ndarray:
    return np.arange(start, start + n, dtype=np.float32)[:, None].repeat(3, 1)


def fake_embed(
    _video: Any, _area: Any, start_step: int = 0, n_steps: int = 0, **_: Any
) -> np.ndarray:
    return rows(start_step, n_steps)


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, np.ndarray] = {}

    def object_size(self, key: str) -> int | None:
        return 1 if key in self.objects else None

    def download(self, key: str, path: str) -> None:
        np.savez(path, x=self.objects[key])

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def settings(n: int) -> Settings:
    return Settings(database_url="postgresql+psycopg://u:p@localhost/db", embed_shards=n)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shards, "POLL_S", 0.0)
    monkeypatch.setattr(shards, "MIN_WAIT_S", 0.05)
    monkeypatch.setattr(shards, "EXTRA_WAIT_S", 0.0)


def test_plan_covers_every_step_once() -> None:
    for n, k in [(5098, 4), (1200, 4), (299, 4), (5645, 3)]:
        p = shards.plan(n, k)
        assert p[0][0] == 0
        assert sum(c for _, c in p) == n
        assert all(a + c == b for (a, c), (b, _) in itertools.pairwise(p))
    assert len(shards.plan(299, 4)) == 1  # under a minute per slice: not worth helpers


def test_helpers_results_are_joined(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = FakeStorage()

    def dispatch(_s: Settings, payload: dict[str, Any]) -> bool:
        for sh in payload["shards"]:  # helpers finish instantly
            store.objects[sh["out"]] = rows(sh["start"], sh["steps"])
        return True

    monkeypatch.setattr(shards, "dispatch", dispatch)
    x = shards.sharded_embeddings(
        settings(4),
        store,
        tmp_path / "p.mp4",
        "k",
        None,
        5098,
        "pre",
        fake_embed,  # type: ignore[arg-type]
    )
    assert x is not None
    assert np.array_equal(x, rows(0, 5098))
    assert store.objects == {}  # helper outputs cleaned up


def test_missing_helper_slice_is_done_locally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FakeStorage()

    def dispatch(_s: Settings, payload: dict[str, Any]) -> bool:
        sh = payload["shards"][0]  # only the first helper delivers
        store.objects[sh["out"]] = rows(sh["start"], sh["steps"])
        return True

    monkeypatch.setattr(shards, "dispatch", dispatch)
    x = shards.sharded_embeddings(
        settings(4),
        store,
        tmp_path / "p.mp4",
        "k",
        None,
        5098,
        "pre",
        fake_embed,  # type: ignore[arg-type]
    )
    assert x is not None
    assert np.array_equal(x, rows(0, 5098))


def test_no_dispatch_means_one_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shards, "dispatch", lambda *_: False)
    args = (FakeStorage(), tmp_path / "p.mp4", "k", None, 5098, "pre", fake_embed)
    assert shards.sharded_embeddings(settings(4), *args) is None  # type: ignore[arg-type]
    assert shards.sharded_embeddings(settings(1), *args) is None  # type: ignore[arg-type]


def test_storage_refusal_waits_for_the_daily_reset() -> None:
    from datetime import UTC, datetime

    from botocore.exceptions import ClientError

    from vball_worker.runner import next_storage_reset, storage_refused

    denied = ClientError(
        {"Error": {"Code": "403"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadObject"
    )
    missing = ClientError(
        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"
    )
    assert storage_refused(denied)
    assert not storage_refused(missing)
    assert not storage_refused(RuntimeError("x"))
    now = datetime(2026, 11, 28, 7, 25, tzinfo=UTC)
    assert next_storage_reset(now) == datetime(2026, 11, 29, 0, 5, tzinfo=UTC)
