"""A storage refusal (e.g. Backblaze's daily download cap) waits for the reset instead of
failing the job."""

from __future__ import annotations


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
