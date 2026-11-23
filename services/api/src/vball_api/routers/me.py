"""The signed-in user's own profile."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from vball_api.deps import CurrentUser, DbSession
from vball_core.db.models import Profile

router = APIRouter(tags=["account"])


class Me(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    quota_storage_bytes: int
    quota_minutes_month: int


@router.get("/me", response_model=Me, operation_id="getMe")
async def get_me(user: CurrentUser, db: DbSession) -> Me:
    profile = (await db.execute(select(Profile).where(Profile.id == user.user_id))).scalar_one()
    return Me(
        id=profile.id,
        email=user.email,
        display_name=profile.display_name,
        quota_storage_bytes=profile.quota_storage_bytes,
        quota_minutes_month=profile.quota_minutes_month,
    )
