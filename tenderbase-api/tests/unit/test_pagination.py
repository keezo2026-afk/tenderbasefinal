"""Unit tests for pagination, query validation and the response envelope."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.common import PaginationMeta, PaginationParams, paginated
from app.schemas.tender import SearchQuery, TenderFilter


def test_pagination_offsets_are_deterministic():
    assert PaginationParams(page=1, page_size=25).offset == 0
    assert PaginationParams(page=3, page_size=25).offset == 50
    assert PaginationParams(page=3, page_size=25).limit == 25


def test_pagination_rejects_out_of_range_values():
    with pytest.raises(ValidationError):
        PaginationParams(page=0)
    with pytest.raises(ValidationError):
        PaginationParams(page_size=0)
    with pytest.raises(ValidationError):
        PaginationParams(page_size=10_000)


def test_pagination_meta_math():
    meta = PaginationMeta.build(page=2, page_size=10, total_items=25)
    assert (meta.total_pages, meta.has_next, meta.has_previous) == (3, True, True)

    last = PaginationMeta.build(page=3, page_size=10, total_items=25)
    assert last.has_next is False

    empty = PaginationMeta.build(page=1, page_size=10, total_items=0)
    assert (empty.total_pages, empty.has_next, empty.has_previous) == (0, False, False)


def test_paginated_helper_builds_envelope():
    response = paginated([1, 2], page=1, page_size=2, total=5, request_id="abc")
    assert response.data == [1, 2]
    assert response.pagination.total_items == 5
    assert response.meta.request_id == "abc"


def test_tender_filter_rejects_inverted_date_ranges():
    with pytest.raises(ValidationError):
        TenderFilter(published_after="2026-09-30", published_before="2026-09-01")
    with pytest.raises(ValidationError):
        TenderFilter(closing_after="2026-09-30", closing_before="2026-09-01")


def test_tender_filter_rejects_inverted_value_range():
    with pytest.raises(ValidationError):
        TenderFilter(min_value=100, max_value=10)


def test_tender_filter_rejects_unknown_sort_keys():
    with pytest.raises(ValidationError):
        TenderFilter(sort="dropped_table")
    assert TenderFilter(sort="-closing_at").sort == "-closing_at"


def test_tender_filter_rejects_unknown_parameters():
    with pytest.raises(ValidationError):
        TenderFilter(unexpected="value")


def test_tender_filter_coerces_enum_values():
    filters = TenderFilter(type="RFQ", status="OPEN")
    assert str(filters.type) == "RFQ"
    assert str(filters.status) == "OPEN"


def test_search_query_requires_a_term():
    with pytest.raises(ValidationError):
        SearchQuery(q="a")
    assert SearchQuery(q="solar installation").sort == "relevance"
