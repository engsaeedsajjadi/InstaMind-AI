"""Content Studio + media upload endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy import select

from app.api.deps import DBSession, TenantScopeDep, require
from app.api.schemas import (
    ApprovalRequest,
    CalendarEventRead,
    ContentCreate,
    ContentRead,
    ContentUpdate,
    MediaAssetRead,
)
from app.core.config import settings
from app.core.exceptions import ValidationError
from app.core.permissions import Permission
from app.modules.audit.service import AuditService
from app.modules.content.models import (
    Content,
    ContentApproval,
    ContentCalendarEvent,
    ContentVersion,
    MediaAsset,
)
from app.modules.storage.backend import build_storage_key, get_storage, validate_upload

router = APIRouter(tags=["content"])

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"IN_REVIEW", "SCHEDULED", "ARCHIVED", "CANCELLED"},
    "IN_REVIEW": {"APPROVED", "DRAFT", "CANCELLED"},
    "APPROVED": {"SCHEDULED", "DRAFT", "CANCELLED"},
    "SCHEDULED": {"PUBLISHING", "DRAFT", "CANCELLED"},
    "PUBLISHING": {"PUBLISHED", "FAILED"},
    "PUBLISHED": {"ARCHIVED"},
    "FAILED": {"DRAFT", "SCHEDULED", "ARCHIVED"},
    "CANCELLED": {"DRAFT", "ARCHIVED"},
    "ARCHIVED": {"DRAFT"},
}


# --------------------------------------------------------------------- media
@router.post(
    "/media",
    response_model=MediaAssetRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.MEDIA_UPLOAD))],
)
async def upload_media(
    scope: TenantScopeDep,
    file: UploadFile = File(...),
    kind: str = Query(default="IMAGE", pattern="^(IMAGE|VIDEO)$"),
) -> MediaAssetRead:
    data = await file.read()
    # Enforce the configured ceiling before buffering anything larger.
    limit = settings.MAX_VIDEO_UPLOAD_BYTES if kind == "VIDEO" else settings.MAX_UPLOAD_BYTES
    if len(data) > limit:
        from app.core.exceptions import PayloadTooLarge

        raise PayloadTooLarge(f"File exceeds the maximum size of {limit // (1024 * 1024)} MB.")

    detected_mime, _ext = validate_upload(
        data=data,
        declared_filename=file.filename or "",
        declared_content_type=file.content_type,
        kind=kind,
    )
    key = build_storage_key(kind=kind, mime=detected_mime, original_filename=file.filename or "upload")
    storage = get_storage()
    stored = await storage.put(key, data, content_type=detected_mime)
    public_url = await storage.public_url(stored.key, ttl_seconds=settings.S3_PRESIGN_TTL_SECONDS)

    asset = MediaAsset(
        workspace_id=scope.workspace_id,
        storage_backend=stored.storage_backend,
        storage_key=stored.key,
        bucket=stored.bucket,
        filename=(file.filename or "upload")[:255],
        content_type=(file.content_type or detected_mime)[:128],
        detected_mime=stored.detected_mime,
        size_bytes=stored.size_bytes,
        checksum_sha256=stored.checksum_sha256,
        kind=kind,
        status="UPLOADED",
        public_url=public_url,
        uploaded_by=scope.context.user_id,
        created_by=scope.context.user_id,
    )
    scope.session.add(asset)
    await scope.session.flush()
    return MediaAssetRead.model_validate(asset)


@router.get("/media", response_model=list[MediaAssetRead], dependencies=[Depends(require(Permission.MEDIA_READ))])
async def list_media(
    scope: TenantScopeDep,
    kind: str | None = Query(default=None, pattern="^(IMAGE|VIDEO)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MediaAssetRead]:
    conditions = [MediaAsset.kind == kind] if kind else []
    rows = await scope.list(
        MediaAsset, *conditions, limit=limit, offset=offset, order_by=MediaAsset.created_at.desc()
    )
    return [MediaAssetRead.model_validate(row) for row in rows]


# ------------------------------------------------------------------- content
@router.post(
    "/contents",
    response_model=ContentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.CONTENT_CREATE))],
)
async def create_content(
    payload: ContentCreate, scope: TenantScopeDep, session: DBSession
) -> ContentRead:
    content = Content(
        title=payload.title,
        caption=payload.caption,
        hashtags=payload.hashtags,
        content_type=payload.content_type,
        media_asset_ids=[str(item) for item in payload.media_asset_ids],
        media_order=[str(item) for item in (payload.media_order or payload.media_asset_ids)],
        campaign_id=payload.campaign_id,
        category_id=payload.category_id,
        brand_profile_id=payload.brand_profile_id,
        alt_text=payload.alt_text,
        scheduled_at=payload.scheduled_at,
        timezone=payload.timezone,
        cover_asset_id=payload.cover_asset_id,
        thumb_offset_ms=payload.thumb_offset_ms,
        share_to_feed=payload.share_to_feed,
        collaborators=payload.collaborators,
        location_id=payload.location_id,
        notes=payload.notes,
        status="SCHEDULED" if payload.scheduled_at else "DRAFT",
    )
    scope.attach(content)
    session.add(content)
    await session.flush()

    session.add(
        ContentVersion(
            workspace_id=scope.workspace_id,
            content_id=content.id,
            version=content.version,
            snapshot=_snapshot(content),
            change_summary="created",
        )
    )
    if content.scheduled_at:
        session.add(
            ContentCalendarEvent(
                workspace_id=scope.workspace_id,
                content_id=content.id,
                event_type="CONTENT",
                title=content.title or content.caption[:80],
                starts_at=content.scheduled_at,
                timezone=content.timezone,
            )
        )
    await session.flush()
    return ContentRead.model_validate(content)


@router.get(
    "/contents",
    response_model=list[ContentRead],
    dependencies=[Depends(require(Permission.CONTENT_READ))],
)
async def list_contents(
    scope: TenantScopeDep,
    status_filter: str | None = Query(default=None, alias="status"),
    campaign_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ContentRead]:
    conditions = []
    if status_filter:
        conditions.append(Content.status == status_filter)
    if campaign_id:
        conditions.append(Content.campaign_id == campaign_id)
    rows = await scope.list(
        Content, *conditions, limit=limit, offset=offset, order_by=Content.created_at.desc()
    )
    return [ContentRead.model_validate(row) for row in rows]


@router.get(
    "/contents/{content_id}",
    response_model=ContentRead,
    dependencies=[Depends(require(Permission.CONTENT_READ))],
)
async def get_content(content_id: uuid.UUID, scope: TenantScopeDep) -> ContentRead:
    content = await scope.get_one(Content, content_id)
    return ContentRead.model_validate(content)


@router.patch(
    "/contents/{content_id}",
    response_model=ContentRead,
    dependencies=[Depends(require(Permission.CONTENT_UPDATE))],
)
async def update_content(
    content_id: uuid.UUID,
    payload: ContentUpdate,
    scope: TenantScopeDep,
    session: DBSession,
) -> ContentRead:
    content = await scope.get_one(Content, content_id)
    data = payload.model_dump(exclude_unset=True)

    if "status" in data and data["status"]:
        target = data["status"]
        if target not in ALLOWED_TRANSITIONS.get(content.status, set()):
            allowed = sorted(ALLOWED_TRANSITIONS.get(content.status, set()))
            raise ValidationError(
                f"Cannot move content from {content.status} to {target}.",
                errors=[
                    {
                        "field": "status",
                        "code": "illegal_state_transition",
                        "message": f"Allowed next states: {', '.join(allowed) or 'none'}.",
                    }
                ],
                context={"from": content.status, "to": target, "allowed": allowed},
            )

    session.add(
        ContentVersion(
            workspace_id=scope.workspace_id,
            content_id=content.id,
            version=content.version,
            snapshot=_snapshot(content),
            change_summary=",".join(sorted(data.keys()))[:255] or "updated",
        )
    )

    for field, value in data.items():
        if field in {"media_asset_ids", "media_order"} and value is not None:
            setattr(content, field, [str(item) for item in value])
        elif value is not None or field in {"alt_text", "notes", "hashtags"}:
            setattr(content, field, value)
    content.version += 1
    scope.touch(content)
    await session.flush()
    return ContentRead.model_validate(content)


@router.delete(
    "/contents/{content_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Permission.CONTENT_DELETE))],
)
async def delete_content(content_id: uuid.UUID, scope: TenantScopeDep) -> None:
    content = await scope.get_one(Content, content_id)
    if content.status in {"PUBLISHING", "PUBLISHED"}:
        raise ValidationError("Published or publishing content cannot be deleted; archive it instead.")
    await scope.soft_delete(content)


@router.post(
    "/contents/{content_id}/submit",
    response_model=ContentRead,
    dependencies=[Depends(require(Permission.CONTENT_SUBMIT))],
)
async def submit_for_review(content_id: uuid.UUID, scope: TenantScopeDep, session: DBSession) -> ContentRead:
    content = await scope.get_one(Content, content_id)
    if content.status != "DRAFT":
        raise ValidationError(f"Only DRAFT content can be submitted (current: {content.status}).")
    content.status = "IN_REVIEW"
    session.add(
        ContentApproval(
            workspace_id=scope.workspace_id,
            content_id=content.id,
            requested_by=scope.context.user_id,
            status="PENDING",
        )
    )
    scope.touch(content)
    await session.flush()
    return ContentRead.model_validate(content)


@router.post(
    "/contents/{content_id}/approve",
    response_model=ContentRead,
    dependencies=[Depends(require(Permission.CONTENT_APPROVE))],
)
async def approve_content(
    content_id: uuid.UUID,
    payload: ApprovalRequest,
    scope: TenantScopeDep,
    session: DBSession,
) -> ContentRead:
    content = await scope.get_one(Content, content_id)
    approval = (
        await session.execute(
            select(ContentApproval)
            .where(
                ContentApproval.workspace_id == scope.workspace_id,
                ContentApproval.content_id == content.id,
                ContentApproval.status == "PENDING",
            )
            .order_by(ContentApproval.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if approval is None:
        raise ValidationError("There is no pending review for this content.")

    approval.status = payload.status
    approval.comment = payload.comment
    approval.reviewer_id = scope.context.user_id
    approval.decided_at = datetime.now(UTC)

    content.status = {
        "APPROVED": "APPROVED",
        "REJECTED": "DRAFT",
        "CHANGES_REQUESTED": "DRAFT",
    }[payload.status]
    scope.touch(content)
    await AuditService(session).record(
        action=f"content.{payload.status.lower()}",
        workspace_id=scope.workspace_id,
        actor_id=scope.context.user_id,
        resource_type="content",
        resource_id=str(content.id),
        after={"approval_status": payload.status},
    )
    await session.flush()
    return ContentRead.model_validate(content)


# ------------------------------------------------------------------ calendar
@router.get(
    "/calendar",
    response_model=list[CalendarEventRead],
    dependencies=[Depends(require(Permission.CONTENT_READ))],
)
async def list_calendar(
    scope: TenantScopeDep,
    start: datetime = Query(...),
    end: datetime = Query(...),
) -> list[CalendarEventRead]:
    if end <= start:
        raise ValidationError("`end` must be after `start`.")
    rows = await scope.list(
        ContentCalendarEvent,
        ContentCalendarEvent.starts_at >= start,
        ContentCalendarEvent.starts_at <= end,
        order_by=ContentCalendarEvent.starts_at.asc(),
        limit=500,
    )
    return [CalendarEventRead.model_validate(row) for row in rows]


def _snapshot(content: Content) -> dict:
    return {
        "title": content.title,
        "caption": content.caption,
        "hashtags": content.hashtags,
        "content_type": content.content_type,
        "media_asset_ids": content.media_asset_ids,
        "media_order": content.media_order,
        "status": content.status,
        "scheduled_at": content.scheduled_at.isoformat() if content.scheduled_at else None,
        "alt_text": content.alt_text,
        "collaborators": content.collaborators,
    }
