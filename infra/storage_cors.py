"""Allow the web app to upload to (and play from) the storage bucket directly.

    uv run python infra/storage_cors.py https://your-app.vercel.app [http://localhost:3100]

Uses the VBALL_S3_* settings (.env or environment). Browsers upload parts with presigned PUTs
and need the ETag header back to finish a multipart upload; playback uses presigned GETs.
Works with Backblaze B2's S3 API (and other S3-compatible stores).
"""

from __future__ import annotations

import sys

import boto3

from vball_core.settings import Settings


def main() -> None:
    origins = sys.argv[1:]
    if not origins:
        raise SystemExit(__doc__)
    s = Settings()
    if s.s3_access_key_id is None or s.s3_secret_access_key is None:
        raise SystemExit("VBALL_S3_ACCESS_KEY_ID / VBALL_S3_SECRET_ACCESS_KEY are not set")
    client = boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=s.s3_secret_access_key.get_secret_value(),
    )
    client.put_bucket_cors(
        Bucket=s.s3_bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": origins,
                    "AllowedMethods": ["GET", "HEAD", "PUT"],
                    "AllowedHeaders": ["*"],
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )
    print("CORS set on", s.s3_bucket, "for", ", ".join(origins))


if __name__ == "__main__":
    main()
