"""Enumerations stored as constrained text columns (CHECK constraints, not native PG enums,
so adding a value is a simple migration)."""

from __future__ import annotations

from enum import StrEnum


class VideoStatus(StrEnum):
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    INGESTING = "ingesting"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class JobKind(StrEnum):
    INGEST = "ingest"
    ANALYZE = "analyze"
    REDETECT = "redetect"
    HIGHLIGHT = "highlight"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobErrorCode(StrEnum):
    INPUT_INVALID = "input_invalid"  # not retryable; message is shown to the user
    TRANSIENT = "transient"  # retried with backoff
    INTERNAL = "internal"  # retried, then failed and reported
    CANCELLED = "cancelled"


class ModelTask(StrEnum):
    COURT = "court"
    PLAYER_DETECTION = "player_detection"
    BALL = "ball"
    RALLY = "rally"
    ACTION = "action"


class ModelStatus(StrEnum):
    CANDIDATE = "candidate"
    PRODUCTION = "production"
    RETIRED = "retired"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CalibrationSource(StrEnum):
    MANUAL = "manual"
    MODEL = "model"


class EditOp(StrEnum):
    ADD = "add"
    DELETE = "delete"
    MODIFY = "modify"


class ExportStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"
