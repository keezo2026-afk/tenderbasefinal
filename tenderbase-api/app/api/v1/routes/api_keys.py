"""API key administration endpoints (``admin`` scope only).

The raw key is returned **once**, in the create response, and is never
retrievable afterwards — the database holds only a keyed digest. Revocation is
immediate: every request re-reads the key row, so there is no cache in which a
revoked credential can survive.

Endpoints here are deliberately excluded from what a ``read:*`` key can reach,
and the create endpoint is additionally disabled unless
``API_KEY_SELF_SERVICE_ENABLED`` is set, so a production deployment mints keys
with the operator script (an auditable, shell-visible action) unless an operator
explicitly opts into API-based minting.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.auth import Principal, api_access
from app.api.dependencies import MetaDep, SessionDep
from app.config import get_settings
from app.db.models.security import ApiKey
from app.schemas.common import DataResponse, ListResponse, PaginationMeta
from app.schemas.security import (
    ApiKeyCreated,
    ApiKeyIssueRequest,
    ApiKeyRead,
    ApiKeyRevokeRequest,
    ApiKeySummary,
)
from app.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def _read(key: ApiKey) -> ApiKeyRead:
    return ApiKeyRead(
        id=key.id,
        name=key.name,
        prefix=key.key_prefix,
        status=str(key.status),  # type: ignore[arg-type]
        scopes=list(key.scopes or []),
        created_at=key.created_at,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        last_used_ip=key.last_used_ip,
        revoked_at=key.revoked_at,
        revoked_reason=key.revoked_reason,
        created_by=key.created_by,
        notes=key.notes,
    )


@router.get(
    "",
    response_model=ListResponse[ApiKeyRead],
    summary="List API keys",
    description=(
        "Every issued key with its scopes and usage metadata. **The key itself "
        "is never included** — only its non-secret prefix. Requires the `admin` scope."
    ),
)
async def list_api_keys(
    session: SessionDep,
    meta: MetaDep,
    _principal: Annotated[Principal, Depends(api_access)],
    include_revoked: Annotated[bool, Query()] = True,
) -> ListResponse[ApiKeyRead]:
    service = ApiKeyService(session)
    keys = await service.list_keys(include_revoked=include_revoked)
    return ListResponse[ApiKeyRead](
        data=[_read(key) for key in keys],
        pagination=PaginationMeta.build(page=1, page_size=max(len(keys), 1), total_items=len(keys)),
        meta=meta,
    )


@router.get(
    "/summary",
    response_model=DataResponse[ApiKeySummary],
    summary="API key counts by status",
)
async def api_key_summary(
    session: SessionDep,
    meta: MetaDep,
    _principal: Annotated[Principal, Depends(api_access)],
) -> DataResponse[ApiKeySummary]:
    service = ApiKeyService(session)
    counts = await service.stats()
    return DataResponse[ApiKeySummary](
        data=ApiKeySummary(
            active=int(counts.get("ACTIVE", 0)),
            revoked=int(counts.get("REVOKED", 0)),
            expired=int(counts.get("EXPIRED", 0)),
        ),
        meta=meta,
    )


@router.post(
    "",
    response_model=DataResponse[ApiKeyCreated],
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key",
    description=(
        "Mints a new key. The response contains the only copy of the secret — "
        "TenderBase stores a keyed digest and cannot show it again.\n\n"
        "Disabled unless `API_KEY_SELF_SERVICE_ENABLED=true`; use "
        "`python -m scripts.manage_api_keys create ...` in production."
    ),
)
async def create_api_key(
    payload: ApiKeyIssueRequest,
    session: SessionDep,
    meta: MetaDep,
    _principal: Annotated[Principal, Depends(api_access)],
    response: Response,
) -> DataResponse[ApiKeyCreated]:
    settings = get_settings()
    if not settings.api_key_self_service_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "API key creation is disabled. Run "
                "`python -m scripts.manage_api_keys create --name ...` on the server, "
                "or set API_KEY_SELF_SERVICE_ENABLED=true to allow it here."
            ),
        )
    service = ApiKeyService(session, settings)
    issued = await service.create(
        name=payload.name,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
        created_by=f"api:{_principal.display_id or 'unknown'}",
        notes=payload.notes,
    )
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return DataResponse[ApiKeyCreated](
        data=ApiKeyCreated(
            id=issued.key_id,
            name=issued.name,
            prefix=issued.prefix,
            key=issued.raw_key,
            scopes=list(issued.scopes),
            created_at=issued.created_at,
            expires_at=issued.expires_at,
        ),
        meta=meta,
    )


@router.post(
    "/{key_id}/revoke",
    response_model=DataResponse[ApiKeyRead],
    summary="Revoke an API key",
    description=(
        "Immediately invalidates the key for every subsequent request. "
        "Takes effect on the next request; there is no credential cache."
    ),
)
async def revoke_api_key(
    key_id: UUID,
    session: SessionDep,
    meta: MetaDep,
    _principal: Annotated[Principal, Depends(api_access)],
    payload: ApiKeyRevokeRequest | None = None,
) -> DataResponse[ApiKeyRead]:
    service = ApiKeyService(session)
    key = await service.revoke(str(key_id), reason=payload.reason if payload else None)
    await session.commit()
    return DataResponse[ApiKeyRead](data=_read(key), meta=meta)


@router.get(
    "/{key_id}",
    response_model=DataResponse[ApiKeyRead],
    summary="Get an API key's metadata",
)
async def get_api_key(
    key_id: UUID,
    session: SessionDep,
    meta: MetaDep,
    _principal: Annotated[Principal, Depends(api_access)],
) -> DataResponse[ApiKeyRead]:
    service = ApiKeyService(session)
    key = await service.get(str(key_id))
    return DataResponse[ApiKeyRead](data=_read(key), meta=meta)
