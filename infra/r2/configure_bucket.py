"""Configure the production bucket (Cloudflare R2) for browser uploads.

Applies, idempotently:
* CORS: the web app's origins may PUT parts and GET/HEAD objects with presigned URLs.
* Lifecycle: abort multipart uploads left unfinished for 7 days (they still count toward the
  10 GB free tier until aborted).

Usage (credentials from the environment, same names the API uses):
    uv run python infra/r2/configure_bucket.py https://your-app.vercel.app http://localhost:3100
"""

from __future__ import annotations

import sys

import boto3
from botocore.config import Config

from vball_core.settings import get_settings


def main(origins: list[str]) -> None:
    if not origins:
        sys.exit("usage: configure_bucket.py ORIGIN [ORIGIN ...]")
    s = get_settings()
    if not (s.s3_access_key_id and s.s3_secret_access_key and s.s3_endpoint_url):
        sys.exit("set VBALL_S3_ENDPOINT_URL, VBALL_S3_ACCESS_KEY_ID, VBALL_S3_SECRET_ACCESS_KEY")
    s3 = boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=s.s3_secret_access_key.get_secret_value(),
        config=Config(signature_version="s3v4"),
    )
    s3.put_bucket_cors(
        Bucket=s.s3_bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": origins,
                    "AllowedMethods": ["PUT", "GET", "HEAD"],
                    "AllowedHeaders": ["*"],
                    "ExposeHeaders": ["ETag", "Content-Length", "Content-Range"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=s.s3_bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "abort-stale-multipart",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                }
            ]
        },
    )
    print(f"bucket {s.s3_bucket}: CORS for {', '.join(origins)}; stale uploads abort after 7 days")


if __name__ == "__main__":
    main(sys.argv[1:])
