"""Add a local video file to the app as if it had been uploaded in the browser (dev helper).

    uv run python -m vball_worker.dev_upload PATH [--name NAME]

Stores the file as the original, creates the video for the (single) owner and queues ingest,
which then queues rally analysis. Uses the same settings as the worker (.env).
"""

from __future__ import annotations

import argparse
import mimetypes
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from vball_core import jobs, pipelines
from vball_core.db.enums import JobKind, VideoStatus
from vball_core.db.models import Video
from vball_core.settings import Settings
from vball_core.storage import ObjectStorage


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--name", help="file name shown in the app (default: the file's name)")
    a = ap.parse_args()
    settings = Settings()
    storage = ObjectStorage(settings)
    engine = create_engine(settings.database_dsn())
    with Session(engine) as session:
        owner = session.scalar(select(Video.owner_id).limit(1))
        if owner is None:
            raise SystemExit("no existing video to take the owner from; upload once in the app")
        video_id = uuid.uuid4()
        ext = a.path.suffix.lower() or ".mp4"
        key = f"originals/{owner}/{video_id}{ext}"
        content_type = mimetypes.guess_type(a.path.name)[0] or "video/mp4"
        print("uploading", a.path.stat().st_size // 2**20, "MB ...", flush=True)
        storage.upload(str(a.path), key, content_type)
        session.add(
            Video(
                id=video_id,
                owner_id=owner,
                filename=a.name or a.path.name,
                size_bytes=a.path.stat().st_size,
                content_type=content_type,
                original_key=key,
                status=VideoStatus.UPLOADED,
            )
        )
        session.flush()
        jobs.enqueue(
            session.connection(),
            video_id=video_id,
            owner_id=owner,
            kind=JobKind.INGEST,
            pipeline_version=pipelines.INGEST,
            params={},
        )
        session.commit()
    print("video", video_id, "queued for ingest")


if __name__ == "__main__":
    main()
