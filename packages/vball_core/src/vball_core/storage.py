"""S3-compatible object storage (Cloudflare R2 in production, SeaweedFS locally).

Videos never pass through the API: the browser uploads parts straight to storage using
presigned URLs, and downloads/plays files through presigned GET URLs. This module is
synchronous (boto3); async callers run it in a worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from vball_core.settings import Settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import CompletedPartTypeDef

MIB = 1024 * 1024
MIN_PART_SIZE = 16 * MIB  # S3 minimum is 5 MiB; 16 MiB balances request count vs. resume cost
MAX_PARTS = 10_000


class StorageNotConfiguredError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UploadedPart:
    part_number: int
    etag: str
    size: int


def part_plan(size_bytes: int) -> tuple[int, int]:
    """(part_size, part_count) for a file. Parts are whole MiB; the last part may be smaller."""
    if size_bytes <= 0:
        raise ValueError("size must be positive")
    per_part = -(-size_bytes // (MAX_PARTS - 1))  # leave headroom below the 10,000 limit
    part_size = max(MIN_PART_SIZE, -(-per_part // MIB) * MIB)
    part_count = -(-size_bytes // part_size)
    return part_size, part_count


def _client(settings: Settings, endpoint: str | None) -> S3Client:
    key_id, secret = settings.s3_access_key_id, settings.s3_secret_access_key
    if key_id is None or secret is None:
        raise StorageNotConfiguredError("object storage credentials are not set")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=key_id.get_secret_value(),
        aws_secret_access_key=secret.get_secret_value(),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 4, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
        ),
    )


class ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self._s3 = _client(settings, settings.s3_endpoint_url)
        public = settings.s3_public_endpoint_url or settings.s3_endpoint_url
        self._presigner = (
            self._s3 if public == settings.s3_endpoint_url else _client(settings, public)
        )

    # --- health / setup -------------------------------------------------------------------
    def check(self) -> None:
        self._s3.head_bucket(Bucket=self.bucket)

    def ensure_bucket(self) -> None:
        """Create the bucket if missing. Only used for local development and tests."""
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._s3.create_bucket(Bucket=self.bucket)

    # --- multipart uploads ----------------------------------------------------------------
    def create_multipart(self, key: str, content_type: str) -> str:
        resp = self._s3.create_multipart_upload(
            Bucket=self.bucket, Key=key, ContentType=content_type
        )
        return resp["UploadId"]

    def presign_part(self, key: str, upload_id: str, part_number: int, expires_s: int) -> str:
        return self._presigner.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=expires_s,
        )

    def list_parts(self, key: str, upload_id: str) -> list[UploadedPart]:
        parts: list[UploadedPart] = []
        marker = 0
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
                "MaxParts": 1000,
            }
            if marker:
                kwargs["PartNumberMarker"] = marker
            resp = self._s3.list_parts(**kwargs)
            for p in resp.get("Parts", []):
                parts.append(UploadedPart(p["PartNumber"], p["ETag"], p["Size"]))
            if not resp.get("IsTruncated"):
                return parts
            marker = int(resp["NextPartNumberMarker"])

    def complete_multipart(self, key: str, upload_id: str, parts: list[UploadedPart]) -> None:
        completed: list[CompletedPartTypeDef] = [
            {"PartNumber": p.part_number, "ETag": p.etag}
            for p in sorted(parts, key=lambda p: p.part_number)
        ]
        self._s3.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": completed},
        )

    def abort_multipart(self, key: str, upload_id: str) -> None:
        try:
            self._s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("NoSuchUpload", "404"):
                raise

    # --- objects --------------------------------------------------------------------------
    def object_size(self, key: str) -> int | None:
        try:
            return self._s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def download(self, key: str, path: str) -> None:
        self._s3.download_file(self.bucket, key, path)

    def upload(self, path: str, key: str, content_type: str) -> None:
        self._s3.upload_file(path, self.bucket, key, ExtraArgs={"ContentType": content_type})

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)

    def presign_get(self, key: str, expires_s: int, download_name: str | None = None) -> str:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if download_name:
            safe = download_name.replace('"', "").replace("\\", "")
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        return self._presigner.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires_s
        )
