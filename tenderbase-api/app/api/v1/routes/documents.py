"""Document endpoints (metadata and extracted text)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query

from app.api.dependencies import DocumentServiceDep, MetaDep, PaginationDep
from app.schemas.common import DataResponse, ErrorResponse, ListResponse, PaginationMeta
from app.schemas.document import DocumentRead, DocumentTextRead, DocumentVersionRead

router = APIRouter(prefix="/documents", tags=["documents"])

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Document not found"}
}


@router.get(
    "",
    response_model=ListResponse[DocumentRead],
    summary="List documents",
    description=(
        "Paginated document metadata across all opportunities, optionally "
        "filtered by opportunity. Documents are identified by the SHA-256 of "
        "their bytes, never by filename."
    ),
)
async def list_documents(
    service: DocumentServiceDep,
    pagination: PaginationDep,
    meta: MetaDep,
    opportunity_id: Annotated[UUID | None, Query(description="Filter by opportunity")] = None,
) -> ListResponse[DocumentRead]:
    documents, total = await service.list_documents(pagination, opportunity_id=opportunity_id)
    return ListResponse[DocumentRead](
        data=[DocumentRead.model_validate(doc) for doc in documents],
        pagination=PaginationMeta.build(
            page=pagination.page, page_size=pagination.page_size, total_items=total
        ),
        meta=meta,
    )


@router.get(
    "/{document_id}",
    response_model=DataResponse[DocumentRead],
    responses=NOT_FOUND,
    summary="Get document metadata",
    description=(
        "Document metadata: source URL, detected format, size, SHA-256 and "
        "download state. The API returns metadata only — the original file "
        "should be retrieved from its official `source_url`."
    ),
)
async def get_document(
    service: DocumentServiceDep,
    meta: MetaDep,
    document_id: Annotated[UUID, Path(description="Document UUID")],
) -> DataResponse[DocumentRead]:
    document = await service.get_document(document_id)
    return DataResponse[DocumentRead](data=DocumentRead.model_validate(document), meta=meta)


@router.get(
    "/{document_id}/versions",
    response_model=ListResponse[DocumentVersionRead],
    responses=NOT_FOUND,
    summary="List document versions",
    description=(
        "Byte-level revision history for a document URL. A new version is "
        "recorded whenever the file's SHA-256 changes."
    ),
)
async def list_document_versions(
    service: DocumentServiceDep,
    meta: MetaDep,
    document_id: Annotated[UUID, Path(description="Document UUID")],
) -> ListResponse[DocumentVersionRead]:
    versions = await service.list_versions(document_id)
    return ListResponse[DocumentVersionRead](
        data=[DocumentVersionRead.model_validate(version) for version in versions],
        pagination=PaginationMeta.build(
            page=1, page_size=max(len(versions), 1), total_items=len(versions)
        ),
        meta=meta,
    )


@router.get(
    "/{document_id}/text",
    response_model=DataResponse[DocumentTextRead],
    responses={
        **NOT_FOUND,
        404: {"model": ErrorResponse, "description": "Document or extracted text not found"},
    },
    summary="Get extracted document text",
    description=(
        "Cleaned text extracted from the document, with provenance: which "
        "method produced it (native PDF, OCR, HTML parse, ...), whether OCR was "
        "used, and an extraction-confidence estimate. Set `include_content=false` "
        "to fetch only the metadata."
    ),
)
async def get_document_text(
    service: DocumentServiceDep,
    meta: MetaDep,
    document_id: Annotated[UUID, Path(description="Document UUID")],
    include_content: Annotated[bool, Query(description="Include the full text body")] = True,
) -> DataResponse[DocumentTextRead]:
    from app.errors import NotFoundError

    text = await service.get_text(document_id)
    if text is None:
        raise NotFoundError(
            "No extracted text is available for this document",
            code="DOCUMENT_TEXT_NOT_FOUND",
        )
    payload = DocumentTextRead.model_validate(text)
    if not include_content:
        payload.content = None
    return DataResponse[DocumentTextRead](data=payload, meta=meta)
