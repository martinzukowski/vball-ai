"""Upload flow end to end: API + Postgres + a real S3-compatible server (SeaweedFS)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import HttpUrl, SecretStr

from authkit import SUPABASE_URL, MockJwks, SigningKey, token
from vball_api.main import create_app
from vball_core.settings import Environment, Settings
from vball_core.storage import MIN_PART_SIZE, ObjectStorage

pytestmark = pytest.mark.db

KEY = SigningKey.ec()


def make_settings(db_url: str, s3: dict[str, str], **over: Any) -> Settings:
    return Settings(
        env=Environment.TEST,
        database_url=SecretStr(db_url),
        log_json=False,
        supabase_url=HttpUrl(SUPABASE_URL),
        s3_endpoint_url=s3["endpoint"],
        s3_access_key_id=SecretStr(s3["key"]),
        s3_secret_access_key=SecretStr(s3["secret"]),
        s3_bucket=s3["bucket"],
        **over,
    )


@pytest.fixture
async def api(migrated_url: str, s3_env: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(make_settings(migrated_url, s3_env), http_client=MockJwks([KEY]).client())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def storage(s3_env: dict[str, str], migrated_url: str) -> ObjectStorage:
    return ObjectStorage(make_settings(migrated_url, s3_env))


def user() -> dict[str, str]:
    return {"Authorization": f"Bearer {token(KEY, sub=str(uuid.uuid4()))}"}


async def create(
    api: httpx.AsyncClient, headers: dict[str, str], size: int, name: str = "match.mov"
) -> dict[str, Any]:
    r = await api.post(
        "/videos/uploads",
        json={"filename": name, "size_bytes": size, "content_type": "video/quicktime"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def put_parts(
    api: httpx.AsyncClient,
    headers: dict[str, str],
    video_id: str,
    data: bytes,
    part_size: int,
    numbers: list[int],
) -> None:
    r = await api.post(
        f"/videos/{video_id}/upload/parts", json={"part_numbers": numbers}, headers=headers
    )
    assert r.status_code == 200, r.text
    async with httpx.AsyncClient() as raw:  # straight to storage, like the browser does
        for item in r.json()["urls"]:
            n = item["part_number"]
            chunk = data[(n - 1) * part_size : n * part_size]
            put = await raw.put(item["url"], content=chunk)
            assert put.status_code == 200, put.text


async def test_full_upload_resume_and_complete(
    api: httpx.AsyncClient, storage: ObjectStorage
) -> None:
    h = user()
    data = bytes(range(256)) * ((MIN_PART_SIZE * 2 + 12345) // 256 + 1)
    data = data[: MIN_PART_SIZE * 2 + 12345]  # 3 parts, last one short
    plan = await create(api, h, len(data))
    vid, ps = plan["video"]["id"], plan["part_size_bytes"]
    assert plan["part_count"] == 3
    assert plan["video"]["status"] == "uploading"

    # Upload part 2 only, then "lose" the browser and ask what storage has.
    await put_parts(api, h, vid, data, ps, [2])
    state = (await api.get(f"/videos/{vid}/upload", headers=h)).json()
    assert [p["part_number"] for p in state["uploaded_parts"]] == [2]

    # Completing early is refused and says what's missing.
    early = await api.post(f"/videos/{vid}/upload/complete", headers=h)
    assert early.status_code == 409
    assert "first missing: 1" in early.json()["detail"]

    await put_parts(api, h, vid, data, ps, [1, 3])
    done = await api.post(f"/videos/{vid}/upload/complete", headers=h)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "uploaded"
    assert storage.object_size(f"originals/{_sub(h)}/{vid}.mov") == len(data)

    # Idempotent, and no more part URLs once complete.
    again = await api.post(f"/videos/{vid}/upload/complete", headers=h)
    assert again.status_code == 200
    late = await api.post(f"/videos/{vid}/upload/parts", json={"part_numbers": [1]}, headers=h)
    assert late.status_code == 409

    listed = (await api.get("/videos", headers=h)).json()
    assert [v["id"] for v in listed["videos"]] == [vid]
    assert listed["storage"]["used_bytes"] >= len(data)

    play = await api.get(f"/videos/{vid}/playback", headers=h)
    assert play.status_code == 200
    assert play.json()["kind"] == "original"
    async with httpx.AsyncClient() as raw:
        got = await raw.get(play.json()["url"], headers={"Range": "bytes=0-9"})
    assert got.content == data[:10]


def _sub(headers: dict[str, str]) -> str:
    import jwt

    return str(
        jwt.decode(headers["Authorization"].split()[1], options={"verify_signature": False})["sub"]
    )


async def test_wrong_size_upload_rejected(api: httpx.AsyncClient) -> None:
    h = user()
    plan = await create(api, h, MIN_PART_SIZE + 10)  # declared 2 parts
    vid, ps = plan["video"]["id"], plan["part_size_bytes"]
    data = b"a" * (MIN_PART_SIZE + 5)  # 5 bytes short
    await put_parts(api, h, vid, data, ps, [1, 2])
    r = await api.post(f"/videos/{vid}/upload/complete", headers=h)
    assert r.status_code == 409
    assert "expected" in r.json()["detail"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"filename": "notes.txt", "size_bytes": 10}, 422),
        ({"filename": "clip.mp4", "size_bytes": 10, "content_type": "text/html"}, 422),
        ({"filename": "clip.mp4", "size_bytes": 0}, 422),
        ({"filename": "clip.mp4", "size_bytes": 21 * 1024**3}, 413),
        ({"filename": "clip.mp4", "size_bytes": 9 * 1024**3 + 1}, 507),
    ],
)
async def test_create_upload_validation(
    api: httpx.AsyncClient, body: dict[str, Any], code: int
) -> None:
    r = await api.post("/videos/uploads", json=body, headers=user())
    assert r.status_code == code, r.text


async def test_filename_is_sanitised(api: httpx.AsyncClient) -> None:
    plan = await create(api, user(), 100, name="..\\..\\evil\x07/../Match 1.MP4")
    assert plan["video"]["filename"] == "Match 1.MP4"


async def test_part_number_bounds(api: httpx.AsyncClient) -> None:
    h = user()
    vid = (await create(api, h, 100))["video"]["id"]
    for numbers in ([0], [2], [1] * 101):
        r = await api.post(f"/videos/{vid}/upload/parts", json={"part_numbers": numbers}, headers=h)
        assert r.status_code == 422, numbers


async def test_other_users_videos_are_invisible(api: httpx.AsyncClient) -> None:
    owner, intruder = user(), user()
    vid = (await create(api, owner, 100))["video"]["id"]
    for method, path, body in [
        ("GET", f"/videos/{vid}", None),
        ("GET", f"/videos/{vid}/upload", None),
        ("POST", f"/videos/{vid}/upload/parts", {"part_numbers": [1]}),
        ("POST", f"/videos/{vid}/upload/complete", None),
        ("GET", f"/videos/{vid}/playback", None),
        ("DELETE", f"/videos/{vid}", None),
    ]:
        r = await api.request(method, path, json=body, headers=intruder)
        assert r.status_code == 404, (method, path, r.status_code)
    assert (await api.get("/videos", headers=intruder)).json()["videos"] == []
    assert (await api.get(f"/videos/{vid}", headers=owner)).status_code == 200


async def test_delete_removes_object_and_hides_video(
    api: httpx.AsyncClient, storage: ObjectStorage
) -> None:
    h = user()
    data = b"z" * 1000
    plan = await create(api, h, len(data))
    vid = plan["video"]["id"]
    await put_parts(api, h, vid, data, plan["part_size_bytes"], [1])
    assert (await api.post(f"/videos/{vid}/upload/complete", headers=h)).status_code == 200
    key = f"originals/{_sub(h)}/{vid}.mov"
    assert storage.object_size(key) == 1000

    assert (await api.delete(f"/videos/{vid}", headers=h)).status_code == 204
    assert storage.object_size(key) is None
    assert (await api.get(f"/videos/{vid}", headers=h)).status_code == 404
    assert (await api.delete(f"/videos/{vid}", headers=h)).status_code == 404


async def test_delete_aborts_unfinished_upload(api: httpx.AsyncClient) -> None:
    h = user()
    plan = await create(api, h, 100)
    assert (await api.delete(f"/videos/{plan['video']['id']}", headers=h)).status_code == 204


async def test_readyz_checks_storage(api: httpx.AsyncClient) -> None:
    r = await api.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"]["storage"]["ok"] is True
