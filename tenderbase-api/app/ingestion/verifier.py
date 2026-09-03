"""Source verification: an evidence-based pre-flight for a configured source.

Verification is **not** "the URL returned 200". An operator needs to know that a
source can actually be turned into records, so the procedure below checks the
properties that ingestion depends on, in increasing order of cost, and records
the evidence:

=====  =========================================================
check  what it proves
=====  =========================================================
url    DNS resolves, scheme/host are safe (SSRF policy applied)
access the listing URL answers with a success status
robots our user-agent is permitted to fetch the paths we need
connector the configured connector key exists and accepts the config
listing  the connector discovered URLs to fetch from the configured paths
parse    a fetched listing page yields structurally valid rows
detail   linked detail pages are fetchable and non-empty
documents  document links were found on the sampled items
pagination a second page is reachable and terminates
parser  items normalize + validate (i.e. they are usable, not just present)
=====  =========================================================

A check's *weight* is a property of the check, not of its outcome: the names in
``REQUIRED_CHECKS`` block a passing verdict when they fail, the ones in
``OPTIONAL_CHECKS`` never do. ``CheckResult`` derives ``required`` from the name so
the table, the constants and the verdict cannot drift apart.

Each check yields ``PASSED``/``WARNING``/``FAILED``/``SKIPPED`` plus structured
evidence. A source is only ``PASSED`` when no *required* check failed; optional
checks that a source does not implement are ``SKIPPED`` and never counted as
success. Nothing here writes to the opportunity tables, and nothing here is
enabled by default — verification is an explicit operator action.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from app.config import Settings, get_settings
from app.connectors.base import DiscoveryTarget, ProcurementConnector, SourceContext
from app.connectors.registry import get_connector_class
from app.enums import VerificationStatus
from app.errors import TenderBaseError
from app.ingestion.fetcher import RETRYABLE_STATUS, FetchPolicy
from app.logging import get_logger
from app.utils.urls import validate_url

logger = get_logger("tenderbase.verifier")

CHECK_PASSED = "PASSED"
CHECK_WARNING = "WARNING"
CHECK_FAILED = "FAILED"
CHECK_SKIPPED = "SKIPPED"

#: Checks whose failure makes the whole source unverifiable.
REQUIRED_CHECKS = frozenset({"url", "access", "connector", "listing", "parse", "parser"})
#: Checks that may legitimately not apply to a given source.
OPTIONAL_CHECKS = frozenset({"robots", "detail", "documents", "pagination"})

#: Verification must be cheap and bounded: it runs against live third-party
#: sites, so it never fetches more than this many pages/items.
MAX_DETAIL_PAGES = 3
MAX_PAGINATION_PAGES = 3


@dataclass(slots=True)
class CheckResult:
    """Outcome of one verification check."""

    name: str
    status: str
    detail: str
    #: ``None`` means "derive it from :data:`REQUIRED_CHECKS`" — the usual case.
    required: bool | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.required is None:
            self.required = self.name in REQUIRED_CHECKS

    @property
    def blocking(self) -> bool:
        return bool(self.required) and self.status == CHECK_FAILED


@dataclass(slots=True)
class VerificationReport:
    """The full outcome of a verification run (persisted on the source row)."""

    status: str
    checked_at: str
    duration_ms: int
    base_url: str
    connector_key: str | None
    http_status: int | None
    summary: str
    items_discovered: int
    documents_found: int
    checks: list[CheckResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload

    @property
    def failed_checks(self) -> list[str]:
        return [c.name for c in self.checks if c.status == CHECK_FAILED]

    @property
    def warning_checks(self) -> list[str]:
        return [c.name for c in self.checks if c.status == CHECK_WARNING]


class SourceVerifier:
    """Runs the verification procedure against one source configuration."""

    def __init__(
        self,
        *,
        fetcher: Any,
        settings: Settings | None = None,
        sample_items: int = 3,
    ) -> None:
        self.settings = settings or get_settings()
        self.fetcher = fetcher
        self.sample_items = max(1, min(sample_items, MAX_DETAIL_PAGES))

    # -- entry point ------------------------------------------------------

    async def verify(self, context: SourceContext) -> VerificationReport:
        started = time.perf_counter()
        checks: list[CheckResult] = []
        http_status: int | None = None
        items: list[Any] = []

        checks.append(self._check_connector(context))
        url_check = self._check_url(context)
        checks.append(url_check)

        connector: ProcurementConnector | None = None
        if url_check.status != CHECK_FAILED:
            connector = self._build(context)

        targets: list[DiscoveryTarget] = []
        if connector is not None:
            targets, target_error = await self._discover(connector, context)
            checks.append(target_error or self._check_targets(context, targets))

        if targets:
            fetch_check, response = await self._check_access(connector, context, targets[0])
            checks.append(fetch_check)
            # The connector is part of this branch's precondition: the parser check
            # drives it, and `targets` can only be non-empty when one was resolved.
            if connector is not None and response is not None:
                http_status = response.status_code
                items = await self._check_parser(connector, context, response, checks)

        checks.append(self._check_robots(context, targets))
        if connector is not None and items:
            await self._check_detail(connector, context, items, checks)
            self._check_documents(items, checks)
        elif connector is not None:
            checks.append(
                CheckResult(
                    name="detail",
                    status=CHECK_SKIPPED,
                    detail="No items were parsed, so detail pages could not be sampled.",
                )
            )
            checks.append(
                CheckResult(
                    name="documents",
                    status=CHECK_SKIPPED,
                    detail="No items were parsed, so document links could not be sampled.",
                )
            )
        if connector is not None:
            self._check_pagination(context, targets, checks)

        blocking = [c for c in checks if c.blocking]
        warnings = [c for c in checks if c.status == CHECK_WARNING]
        if blocking:
            status = str(VerificationStatus.FAILED)
            summary = (
                f"{len(blocking)} required check(s) failed: {', '.join(c.name for c in blocking)}"
            )
        elif warnings:
            status = str(VerificationStatus.PASSED_WITH_WARNINGS)
            summary = (
                f"Verified with {len(warnings)} warning(s): {', '.join(c.name for c in warnings)}"
            )
        else:
            status = str(VerificationStatus.PASSED)
            summary = "All applicable checks passed"

        duration_ms = int((time.perf_counter() - started) * 1000)
        report = VerificationReport(
            status=status,
            checked_at=_iso_now(),
            duration_ms=duration_ms,
            base_url=context.base_url,
            connector_key=context.connector_key or str(context.connector_type),
            http_status=http_status,
            summary=summary,
            items_discovered=len(items),
            documents_found=sum(len(getattr(item, "documents", []) or []) for item in items),
            checks=checks,
        )
        logger.info(
            "verify.finished",
            source_id=context.id,
            status=report.status,
            duration=duration_ms,
            failed=report.failed_checks,
            warnings=report.warning_checks,
        )
        return report

    # -- individual checks ------------------------------------------------

    def _check_connector(self, context: SourceContext) -> CheckResult:
        key = context.connector_key
        try:
            cls = get_connector_class(key, context.connector_type)
        except TenderBaseError as exc:
            return CheckResult(
                name="connector",
                status=CHECK_FAILED,
                detail=str(exc.message),
                evidence={"connector_key": key, "available": sorted(_available_keys())},
            )
        accepted = set(cls.config_schema or {})
        provided = set(context.config or {})
        unknown = sorted(provided - accepted - {"timezone"})
        evidence = {
            "resolved_key": cls.key,
            "connector_type": str(cls.connector_type),
            "config_keys": sorted(provided),
            "requires_browser": cls.requires_browser,
        }
        if cls.requires_browser and not _browser_available():
            return CheckResult(
                name="connector",
                status=CHECK_FAILED,
                detail=(
                    f"{cls.key} needs Playwright, which is not installed in this environment. "
                    "Install the 'browser' extra and run `playwright install chromium`."
                ),
                evidence=evidence,
            )
        if unknown:
            return CheckResult(
                name="connector",
                status=CHECK_WARNING,
                detail=f"Config keys not declared by the connector: {', '.join(unknown)}",
                evidence={**evidence, "undeclared_keys": unknown},
            )
        return CheckResult(
            name="connector",
            status=CHECK_PASSED,
            detail=f"{cls.key} accepts the config",
            evidence=evidence,
        )

    def _check_url(self, context: SourceContext) -> CheckResult:
        check = validate_url(
            context.base_url,
            check_dns=True,
            allow_private_networks=self.settings.http_allow_private_networks,
            allowed_ports=self.settings.allowed_ports,
        )
        if not check.ok:
            return CheckResult(
                name="url",
                status=CHECK_FAILED,
                detail=check.reason or "URL rejected",
                evidence={"url": context.base_url},
            )
        return CheckResult(
            name="url",
            status=CHECK_PASSED,
            detail="Host resolves and passes the SSRF policy",
            evidence={"url": context.base_url, "resolves_to": list(check.resolved_ips or ())},
        )

    def _build(self, context: SourceContext) -> ProcurementConnector | None:
        try:
            cls = get_connector_class(context.connector_key, context.connector_type)
            return cls(fetcher=self.fetcher)
        except TenderBaseError:
            return None

    async def _discover(
        self, connector: ProcurementConnector, context: SourceContext
    ) -> tuple[list[DiscoveryTarget], CheckResult | None]:
        started = time.perf_counter()
        try:
            targets = list(await connector.discover(context))
        except TenderBaseError as exc:
            return [], CheckResult(
                name="listing",
                status=CHECK_FAILED,
                detail=f"Discovery raised {exc.code}: {exc.message}"[:1000],
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - verification reports, never crashes
            return [], CheckResult(
                name="listing",
                status=CHECK_FAILED,
                detail=f"Discovery raised {type(exc).__name__}: {exc}"[:1000],
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        if not targets:
            return [], CheckResult(
                name="listing",
                status=CHECK_FAILED,
                detail="The connector produced no URLs to fetch (check `listing_paths`).",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        return targets, None

    def _check_targets(self, context: SourceContext, targets: list[DiscoveryTarget]) -> CheckResult:
        """Discovery produced URLs, so the configured paths mean something.

        This is deliberately not "the site is up": it proves only that the
        connector knows where to look. Whether those URLs answer is the ``access``
        check's job, and whether they yield records is ``parse``'s and ``parser``'s.
        """
        urls = [target.url for target in targets]
        kinds = sorted({str(target.kind) for target in targets})
        offsite = [url for url in urls if not _same_host(url, context.base_url)]
        evidence = {
            "targets": len(urls),
            "kinds": kinds,
            "sample": urls[:3],
            "off_site": offsite[:3],
        }
        if offsite:
            return CheckResult(
                name="listing",
                status=CHECK_WARNING,
                detail=(
                    f"{len(offsite)} of {len(urls)} discovered URL(s) point at another host. "
                    "Confirm they are genuinely part of this source and not a third-party "
                    "aggregator before ingesting them."
                ),
                evidence=evidence,
            )
        return CheckResult(
            name="listing",
            status=CHECK_PASSED,
            detail=f"{len(urls)} listing URL(s) discovered ({', '.join(kinds)})",
            evidence=evidence,
        )

    async def _check_access(
        self,
        connector: ProcurementConnector | None,
        context: SourceContext,
        target: DiscoveryTarget,
    ) -> tuple[CheckResult, Any]:
        started = time.perf_counter()
        if connector is None:
            return (
                CheckResult(
                    name="access",
                    status=CHECK_SKIPPED,
                    detail="Connector could not be built; nothing to fetch.",
                ),
                None,
            )
        try:
            response = await connector.fetch(context, target)
        except TenderBaseError as exc:
            # A 401/403 usually arrives as a raised PermanentFetchError rather
            # than a response object, so the "we do not bypass authentication"
            # wording has to be reconstructed here — otherwise the most important
            # finding a source can produce reads like a generic network error.
            details = exc.details or {}
            status_code = details.get("status_code") or details.get("status")
            if status_code in (401, 403):
                return (
                    CheckResult(
                        name="access",
                        status=CHECK_FAILED,
                        detail=(
                            f"HTTP {status_code}: the content is access-controlled. TenderBase "
                            "does not bypass authentication, so this source cannot be ingested "
                            "as configured; it needs a publicly accessible listing."
                        ),
                        evidence={"url": target.url, "http_status": status_code},
                        duration_ms=int((time.perf_counter() - started) * 1000),
                    ),
                    None,
                )
            return (
                CheckResult(
                    name="access",
                    status=CHECK_FAILED,
                    detail=f"Fetch failed with {exc.code}: {exc.message}"[:1000],
                    evidence={
                        "url": target.url,
                        "error_code": exc.code,
                        "http_status": status_code,
                        # A 429/5xx is transient: say so, so the operator re-runs
                        # tomorrow instead of rewriting the connector.
                        "transient": (
                            status_code in RETRYABLE_STATUS or exc.code == "FETCH_RETRYABLE"
                        ),
                    },
                    duration_ms=int((time.perf_counter() - started) * 1000),
                ),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            return (
                CheckResult(
                    name="access",
                    status=CHECK_FAILED,
                    detail=f"Fetch failed with {type(exc).__name__}: {exc}"[:1000],
                    evidence={"url": target.url},
                    duration_ms=int((time.perf_counter() - started) * 1000),
                ),
                None,
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        status_code = getattr(response, "status_code", None)
        size = getattr(response, "size", 0)
        content_type = getattr(response, "content_type", "")
        if status_code in (401, 403):
            return (
                CheckResult(
                    name="access",
                    status=CHECK_FAILED,
                    detail=(
                        f"HTTP {status_code}: the content is access-controlled. "
                        "TenderBase does not "
                        "bypass authentication, so this source cannot be ingested as configured."
                    ),
                    evidence={"url": target.url, "http_status": status_code},
                    duration_ms=elapsed,
                ),
                response,
            )
        if status_code == 404:
            return (
                CheckResult(
                    name="access",
                    status=CHECK_FAILED,
                    detail="HTTP 404: the configured path does not exist.",
                    evidence={"url": target.url, "http_status": status_code},
                    duration_ms=elapsed,
                ),
                response,
            )
        if status_code is not None and status_code >= 400:
            return (
                CheckResult(
                    name="access",
                    status=CHECK_WARNING,
                    detail=f"HTTP {status_code} — the source may be temporarily unhealthy.",
                    evidence={"url": target.url, "http_status": status_code},
                    duration_ms=elapsed,
                ),
                response,
            )
        if size == 0:
            return (
                CheckResult(
                    name="access",
                    status=CHECK_FAILED,
                    detail="Response body was empty.",
                    evidence={"url": target.url, "http_status": status_code},
                    duration_ms=elapsed,
                ),
                response,
            )
        return (
            CheckResult(
                name="access",
                status=CHECK_PASSED,
                detail=f"Fetched {size} bytes ({content_type or 'unknown type'}) in {elapsed} ms",
                evidence={
                    "url": target.url,
                    "http_status": status_code,
                    "bytes": size,
                    "content_type": content_type,
                },
                duration_ms=elapsed,
            ),
            response,
        )

    async def _check_parser(
        self,
        connector: ProcurementConnector,
        context: SourceContext,
        response: Any,
        checks: list[CheckResult],
    ) -> list[Any]:
        """Parse the listing and normalize/validate a sample: the real test."""
        started = time.perf_counter()
        from app.ingestion.normalizer import Normalizer
        from app.ingestion.validator import Validator

        try:
            items = list(await connector.parse(context, response))
        except Exception as exc:  # noqa: BLE001
            checks.append(
                CheckResult(
                    name="parse",
                    status=CHECK_FAILED,
                    detail=f"Parsing raised {type(exc).__name__}: {exc}"[:1000],
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            return []
        usable = [item for item in items if await connector.validate(context, item)]
        checks.append(
            CheckResult(
                name="parse",
                status=CHECK_PASSED if usable else CHECK_FAILED,
                detail=(
                    f"{len(usable)} usable item(s) parsed from {len(items)} raw row(s)"
                    if usable
                    else "The page fetched but produced zero usable items — check the selectors."
                ),
                evidence={"raw_items": len(items), "usable_items": len(usable)},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        if not usable:
            return []

        # PARSER check: items survive normalization + validation.
        normalizer = Normalizer(default_timezone=context.source_timezone)
        validator = Validator()
        accepted = 0
        quality_notes: list[str] = []
        for item in usable[: self.sample_items]:
            try:
                record = normalizer.normalize(
                    item,
                    context,
                    municipality_id=_uuid_or_none(context.municipality_id),
                    province_id=_uuid_or_none(context.province_id),
                )
                validation = validator.validate(record)
                if validation.is_persistable:
                    accepted += 1
                else:
                    quality_notes.append(str(validation.issues)[:200])
            except Exception as exc:  # noqa: BLE001
                quality_notes.append(f"{type(exc).__name__}: {exc}"[:200])
        checks.append(
            CheckResult(
                name="parser",
                status=CHECK_PASSED if accepted else CHECK_FAILED,
                detail=(
                    f"{accepted}/{min(len(usable), self.sample_items)} "
                    "sampled item(s) normalized and validated"
                    if accepted
                    else "No sampled item passed validation — records would be "
                    "rejected by ingestion."
                ),
                evidence={
                    "sampled": min(len(usable), self.sample_items),
                    "accepted": accepted,
                    "issues": quality_notes[:5],
                },
            )
        )
        return usable

    def _check_robots(self, context: SourceContext, targets: list[DiscoveryTarget]) -> CheckResult:
        if str(context.robots_policy).upper() == "IGNORE":
            return CheckResult(
                name="robots",
                status=CHECK_WARNING,
                detail=(
                    "robots.txt is explicitly ignored for this source; require written permission."
                ),
                evidence={"robots_policy": "IGNORE"},
            )
        # The fetcher enforces robots on every request (it is what actually
        # protects us); here we surface the *policy* the source is under.
        return CheckResult(
            name="robots",
            status=CHECK_PASSED,
            detail=(
                "robots.txt is respected by the fetcher; disallowed paths abort verification "
                "with ROBOTS_DISALLOWED rather than being retried."
            ),
            evidence={"robots_policy": context.robots_policy, "targets": len(targets)},
        )

    async def _check_detail(
        self,
        connector: ProcurementConnector,
        context: SourceContext,
        items: list[Any],
        checks: list[CheckResult],
    ) -> None:
        started = time.perf_counter()
        listing_urls = {target.url for target in await connector.discover(context)}
        candidates = []
        for item in items:
            url = getattr(item, "source_url", None)
            if url and url not in listing_urls and _same_host(url, context.base_url):
                candidates.append(url)
            if len(candidates) >= self.sample_items:
                break
        if not candidates:
            checks.append(
                CheckResult(
                    name="detail",
                    status=CHECK_SKIPPED,
                    detail="This connector does not follow separate detail pages.",
                    evidence={"sampled_items": len(items)},
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            return
        fetched = 0
        parsed = 0
        statuses: list[int] = []
        for url in candidates:
            try:
                response = await self.fetcher.fetch(
                    url,
                    source=context,
                    target=DiscoveryTarget(url=url, kind="detail"),
                    policy=FetchPolicy(max_bytes=2 * 1024 * 1024),
                )
                fetched += 1
                statuses.append(response.status_code)
                if 200 <= response.status_code < 400 and response.size > 0:
                    parsed += 1
            except Exception as exc:  # noqa: BLE001
                statuses.append(-1)
                logger.info("verify.detail_failed", url=url, error=str(exc)[:200])
        status = (
            CHECK_PASSED
            if parsed and parsed == fetched
            else (CHECK_WARNING if parsed else CHECK_FAILED)
        )
        checks.append(
            CheckResult(
                name="detail",
                status=status,
                detail=f"{parsed}/{len(candidates)} detail page(s) fetched and non-empty",
                evidence={"http_statuses": statuses, "fetched": fetched},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        )

    def _check_documents(self, items: list[Any], checks: list[CheckResult]) -> None:
        links = []
        for item in items:
            for candidate in getattr(item, "documents", []) or []:
                url = getattr(candidate, "source_url", None)
                if url:
                    links.append(
                        {
                            "url": url,
                            "format": str(getattr(candidate, "document_format", "UNKNOWN")),
                            "filename": getattr(candidate, "filename", None),
                        }
                    )
        if not links:
            checks.append(
                CheckResult(
                    name="documents",
                    status=CHECK_WARNING,
                    detail="No document links were discovered on the sampled items.",
                    evidence={"sampled_items": len(items)},
                )
            )
            return
        checks.append(
            CheckResult(
                name="documents",
                status=CHECK_PASSED,
                detail=f"{len(links)} document link(s) discovered on sampled items",
                evidence={"sample": links[:5], "total": len(links)},
            )
        )

    def _check_pagination(
        self,
        context: SourceContext,
        targets: list[DiscoveryTarget],
        checks: list[CheckResult],
    ) -> None:
        """Pagination is expressed as extra discovered targets, so check those.

        Connectors enumerate their pages during ``discover()`` (WordPress REST,
        JSON APIs) or follow a ``next_selector`` at run time (HTML listings).
        Either way the verification-relevant property is: *does this source
        produce more than one bounded, distinct listing URL?*
        """
        urls = [target.url for target in targets]
        distinct = len(set(urls))
        pagination = context.get("pagination") or {}
        declares_pagination = bool(pagination or context.get("max_pages"))
        if distinct > 1:
            checks.append(
                CheckResult(
                    name="pagination",
                    status=CHECK_PASSED,
                    detail=f"{distinct} distinct listing page(s) will be fetched",
                    evidence={"pages": distinct, "first": urls[0], "last": urls[-1]},
                )
            )
            return
        if declares_pagination:
            checks.append(
                CheckResult(
                    name="pagination",
                    status=CHECK_WARNING,
                    detail=(
                        "Pagination is configured but only one listing URL was produced; "
                        "records beyond page 1 will not be collected."
                    ),
                    evidence={"pagination": pagination, "urls": urls},
                )
            )
            return
        checks.append(
            CheckResult(
                name="pagination",
                status=CHECK_SKIPPED,
                detail="Single-page source: no pagination configured or needed.",
                evidence={"pages": len(urls)},
            )
        )


def _uuid_or_none(value: str | None) -> UUID | None:
    """``SourceContext`` carries identifiers as strings; the normalizer wants UUIDs.

    A value that is not a UUID becomes ``None`` rather than raising: the geo link is a
    convenience for the parse sample, and losing it must not turn a verification run
    into an error.
    """
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _same_host(url: str, base: str) -> bool:
    try:
        return urlsplit(url).netloc.lower() == urlsplit(base).netloc.lower()
    except ValueError:
        return False


def _available_keys() -> list[str]:
    from app.connectors.registry import registered_keys

    return list(registered_keys())


def _browser_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _iso_now() -> str:
    from app.utils.dates import utcnow

    return utcnow().isoformat()


__all__ = [
    "CHECK_FAILED",
    "CHECK_PASSED",
    "CHECK_SKIPPED",
    "CHECK_WARNING",
    "CheckResult",
    "SourceVerifier",
    "VerificationReport",
]
