"""Split the slow part of rally analysis (DINOv2 frame embeddings) across machines.

The analyze job embeds the first slice itself and asks GitHub Actions
(.github/workflows/embed.yml, via ``repository_dispatch``) to embed the other slices in
parallel; each helper streams only its slice of the proxy from storage and uploads its rows.
Every step is embedded independently, so the joined result is identical to one pass (checked
row for row). If a helper fails or is late, the job embeds that slice itself, so the result
never depends on the helpers, only the speed does.

    python -m vball_worker.shards VIDEO_KEY START_STEP N_STEPS OUT_KEY [PLAY_AREA_JSON]
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from vball_core.logging import configure_logging, get_logger
from vball_core.settings import Settings, get_settings
from vball_core.storage import ObjectStorage

log = get_logger("vball_worker.shards")
POLL_S = 10.0
MIN_WAIT_S = 180.0  # helpers need ~1 min to start
EXTRA_WAIT_S = 120.0


def plan(n_steps: int, shards: int) -> list[tuple[int, int]]:
    """(start_step, n_steps) per slice, as even as possible; slice 0 is done locally."""
    shards = max(1, min(shards, n_steps // 300 or 1))  # no point in slices under a minute
    size = -(-n_steps // shards)
    return [(s, min(size, n_steps - s)) for s in range(0, n_steps, size)]


def dispatch(settings: Settings, payload: dict[str, Any]) -> bool:
    if not settings.worker_dispatch_repo or settings.worker_dispatch_token is None:
        return False
    try:
        r = httpx.post(
            f"https://api.github.com/repos/{settings.worker_dispatch_repo}/dispatches",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {settings.worker_dispatch_token.get_secret_value()}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"event_type": "embed", "client_payload": payload},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        log.warning("embed_dispatch_failed", error=type(exc).__name__)
        return False
    if r.status_code != 204:
        log.warning("embed_dispatch_rejected", status=r.status_code)
        return False
    return True


def sharded_embeddings(
    settings: Settings,
    storage: ObjectStorage,
    proxy: Path,
    proxy_key: str,
    play_area: Sequence[Sequence[float]] | None,
    n_steps: int,
    out_prefix: str,
    embed: Callable[..., np.ndarray],
    on_progress: Callable[[float], None] | None = None,
) -> np.ndarray | None:
    """Embeddings for the whole video using helpers, or None when sharding is off (the caller
    then embeds in one pass). ``embed`` is ``vball_ml.features.video_embeddings``."""
    slices = plan(n_steps, settings.embed_shards)
    if len(slices) < 2:
        return None
    remote: list[dict[str, Any]] = [
        {"start": s, "steps": n, "out": f"{out_prefix}/emb_{i}.npz"}
        for i, (s, n) in enumerate(slices)
        if i > 0
    ]
    if not dispatch(settings, {"video": proxy_key, "play_area": play_area, "shards": remote}):
        return None
    log.info("embed_sharded", slices=len(slices), steps=n_steps)
    started = time.monotonic()
    first = embed(
        proxy,
        play_area,
        start_step=0,
        n_steps=slices[0][1],
        on_progress=on_progress,
        expected_steps=slices[0][1],
    )
    local_s = time.monotonic() - started
    # Helpers need ~1 min to start; give them the local slice's time again plus that margin.
    deadline = time.monotonic() + max(MIN_WAIT_S, local_s + EXTRA_WAIT_S)
    parts: dict[int, np.ndarray] = {0: first}
    with tempfile.TemporaryDirectory() as tmp:
        while len(parts) < len(slices) and time.monotonic() < deadline:
            for i, sh in enumerate(remote, start=1):
                if i in parts or storage.object_size(sh["out"]) is None:
                    continue
                path = Path(tmp) / f"{i}.npz"
                storage.download(sh["out"], str(path))
                rows = np.load(path)["x"]
                if len(rows) == sh["steps"]:
                    parts[i] = rows
                storage.delete(sh["out"])
            if len(parts) < len(slices):
                time.sleep(POLL_S)
    missing = [i for i in range(1, len(slices)) if i not in parts]
    for i in missing:  # helper failed or is late: do it here
        s, n = slices[i]
        parts[i] = embed(proxy, play_area, start_step=s, n_steps=n)
    log.info(
        "embed_joined",
        from_helpers=len(slices) - 1 - len(missing),
        done_locally=len(missing) + 1,
        wall_s=round(time.monotonic() - started, 1),
    )
    return np.concatenate([parts[i] for i in range(len(slices))])


def main() -> None:
    """Helper side: embed one slice of a stored proxy and upload the rows."""
    from vball_ml.features import video_embeddings

    video_key, start, steps, out_key = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    play_area = json.loads(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    storage = ObjectStorage(settings)
    url = storage.presign_get(video_key, 3600)
    t = time.monotonic()
    rows = video_embeddings(url, play_area, start_step=start, n_steps=steps)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "emb.npz"
        np.savez_compressed(path, x=rows)
        storage.upload(str(path), out_key, "application/octet-stream")
    log.info("embed_shard_done", steps=len(rows), wall_s=round(time.monotonic() - t, 1))


if __name__ == "__main__":
    main()
