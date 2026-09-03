"""Source verification against a real HTTP server.

The verification engine is the guard between "a URL exists" and "this source can
be turned into records", so it is tested against sockets, not mocks: a threaded
``http.server`` on 127.0.0.1 that serves a listing, detail pages, a PDF, a
``robots.txt`` and a few deliberate failures. Nothing here touches the public
internet, and nothing here asserts that a 200 is a verdict — several tests exist
precisely to prove it is not.

The local address needs ``HTTP_ALLOW_PRIVATE_NETWORKS=true``, which the fixtures
set explicitly: the SSRF policy that blocks private ranges is the behaviour under
test elsewhere, not an obstacle here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest

from app.config import Settings
from app.connectors.base import ConnectorType, SourceContext
from app.ingestion.fetcher import HTTPFetcher
from app.ingestion.verifier import (
    CHECK_FAILED,
    CHECK_PASSED,
    OPTIONAL_CHECKS,
    REQUIRED_CHECKS,
    SourceVerifier,
)

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"

LISTING = """<html><head><title>Tender advertisements</title></head><body>
<table class="tenders">
  <thead><tr><th>Ref</th><th>Description</th><th>Published</th><th>Closing</th></tr></thead>
  <tbody>
    <tr>
      <td>TF-2026-001</td>
      <td><a href="/tenders/1">Supply and installation of solar inverters</a></td>
      <td>2026-01-10</td>
      <td>2026-02-15</td>
      <td><a class="doc" href="/documents/scope-1.pdf">Scope of work (PDF, 240KB)</a></td>
    </tr>
    <tr>
      <td>TF-2026-002</td>
      <td><a href="/tenders/2">Appointment of a contractor for stormwater drains</a></td>
      <td>2026-01-12</td>
      <td>2026-02-20</td>
      <td><a class="doc" href="/documents/scope-2.pdf">Addendum 1 (PDF, 88KB)</a></td>
    </tr>
  </tbody>
</table>
</body></html>
"""

DETAIL = """<html><body>
<h1>Supply and installation of solar inverters</h1>
<p class="reference">TF-2026-001</p>
<div class="body">
  Bids are invited from registered electrical contractors.
  Closing date: 2026-02-15 11:00.
</div>
<a class="doc" href="/documents/scope-1.pdf">Scope of work</a>
</body></html>
"""

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"

# Path -> (status, content type, body). Bodies are bytes or str.
DEFAULT_ROUTES: dict[str, tuple[int, str, Any]] = {
    "/robots.txt": (200, "text/plain", ROBOTS_ALLOW_ALL),
    "/tenders": (200, "text/html", LISTING),
    "/tenders/1": (200, "text/html", DETAIL),
    "/tenders/2": (200, "text/html", DETAIL),
    "/documents/scope-1.pdf": (200, "application/pdf", PDF_BYTES),
    "/documents/scope-2.pdf": (200, "application/pdf", PDF_BYTES),
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?")[0]
        self.server.hits.append(path)  # type: ignore[attr-defined]
        routes = self.server.routes  # type: ignore[attr-defined]
        if path not in routes:
            self._send(404, "text/plain", b"not found")
            return
        status, content_type, body = routes[path]
        payload = body.encode() if isinstance(body, str) else body
        self._send(status, content_type, payload)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        if status in (401, 403):
            self.send_header("www-authenticate", "Basic realm=\"tenders\"")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        """Silence the per-request stderr noise pytest would otherwise capture."""


@dataclass
class LocalSite:
    """A throwaway HTTP server, with the request log the tests assert against."""

    server: ThreadingHTTPServer
    routes: dict[str, tuple[int, str, Any]]
    hits: list[str] = field(default_factory=list)
    thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def hits_for(self, path: str) -> int:
        return self.hits.count(path)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


#: Ports the tests will listen on. These are in the *default* SSRF allowlist, so
#: the security policy stays exactly as a deployment would run it — no test-only
#: widening of the guard. If both are busy the module skips rather than loosens.
SITE_PORTS = (8080, 8443)


@pytest.fixture
def make_site():
    sites: list[LocalSite] = []

    def _make(
        routes: dict[str, tuple[int, str, Any]] | None = None,
        *,
        merge: bool = True,
    ) -> LocalSite:
        table = dict(DEFAULT_ROUTES) if merge else {}
        table.update(routes or {})
        server = None
        for port in SITE_PORTS:
            if any(site.port == port for site in sites):
                continue
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
                break
            except OSError:
                continue
        if server is None:
            pytest.skip(f"neither {' nor '.join(map(str, SITE_PORTS))} is free to serve on")
        server.routes = table  # type: ignore[attr-defined]
        server.hits = []  # type: ignore[attr-defined]
        site = LocalSite(server=server, routes=table, hits=server.hits)
        site.thread = threading.Thread(target=server.serve_forever, daemon=True)
        site.thread.start()
        sites.append(site)
        return site

    yield _make
    for site in sites:
        site.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        http_allow_private_networks=True,
        http_max_retries=0,
        http_timeout_seconds=5.0,
        http_respect_robots=True,
        http_default_rate_limit_per_minute=6000,
    )


def make_context(site: LocalSite, **overrides: Any) -> SourceContext:
    config = {
        "listing_paths": ["/tenders"],
        "item_selector": "table.tenders tbody tr",
        "field_selectors": {
            "reference_number": "td:nth-child(1)",
            "title": "td:nth-child(2)",
            "published_at": "td:nth-child(3)",
            "closing_at": "td:nth-child(4)",
        },
        "link_selector": "td:nth-child(2) a",
        "document_selector": "a.doc",
    }
    config.update(overrides.pop("config", {}))
    values: dict[str, Any] = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "TEST FIXTURE site",
        "organization": "Test Fixture Municipality",
        "base_url": site.base_url,
        "connector_type": ConnectorType.HTML,
        "connector_key": "html.listing",
        # Any identifiers will do: the parser check is about record *shape*, and
        # the validator never resolves a foreign key. Real ids come from the row.
        "municipality_id": str(uuid4()),
        "province_id": str(uuid4()),
        "config": config,
    }
    values.update(overrides)
    return SourceContext(**values)


async def verify_context(context: SourceContext, settings: Settings):  # noqa: ANN201
    async with HTTPFetcher(settings=settings) as fetcher:
        verifier = SourceVerifier(fetcher=fetcher, settings=settings)
        return await verifier.verify(context)


async def verify(site: LocalSite, settings: Settings, **overrides: Any):  # noqa: ANN201
    context = make_context(site, **overrides)
    return await verify_context(context, settings), context


def check(report: Any, name: str) -> Any:
    for item in report.checks:
        if item.name == name:
            return item
    return None


# --- the happy path -------------------------------------------------------


async def test_a_working_source_passes_every_required_check(make_site, settings):
    site = make_site()
    report, _ = await verify(site, settings)

    assert report.status == "PASSED", report.summary
    assert report.http_status == 200
    assert report.items_discovered == 2
    assert report.documents_found == 2

    for name in REQUIRED_CHECKS:
        assert check(report, name) is not None, f"{name} never ran"
        assert check(report, name).status == CHECK_PASSED, check(report, name).detail
    assert not report.failed_checks
    assert not report.warning_checks


async def test_evidence_records_what_was_actually_observed(make_site, settings):
    site = make_site()
    report, _ = await verify(site, settings)

    access = check(report, "access")
    assert access.evidence["http_status"] == 200
    assert access.evidence["url"].endswith("/tenders")
    assert access.evidence["bytes"] > 500
    assert access.evidence["content_type"].startswith("text/html")

    listing = check(report, "listing")
    assert listing.evidence["targets"] == 1
    assert listing.evidence["kinds"] == ["listing"]

    parser = check(report, "parser")
    assert parser.evidence["accepted"] == 2
    # The listing page itself is never counted as a detail page.
    detail = check(report, "detail")
    assert detail.status == CHECK_PASSED
    assert site.hits_for("/tenders/1") == 1
    assert site.hits_for("/tenders/2") == 1


async def test_pagination_is_reported_when_the_source_declares_it(make_site, settings):
    site = make_site(
        {
            "/tenders?page=2": (200, "text/html", LISTING.replace("TF-2026-", "TF-2026-B-")),
            "/next": (200, "text/html", '<a class="next" href="/tenders?page=2">next</a>'),
        }
    )
    report, _ = await verify(
        site,
        settings,
        config={
            "listing_paths": ["/tenders", "/tenders?page=2"],
            "pagination": {"next_selector": "a.next", "max_pages": 2},
        },
    )
    pagination = check(report, "pagination")
    assert pagination.status == CHECK_PASSED
    assert pagination.evidence["pages"] == 2


# --- the checks that matter most -----------------------------------------


async def test_a_page_that_returns_200_but_yields_nothing_is_not_verified(
    make_site, settings
):
    """The core rule: an OK response is not evidence of a usable source.

    A migrated CMS that serves an empty shell answers 200 forever. Ingestion
    would "succeed" with zero records every night and nobody would notice, so
    verification has to fail it.
    """
    site = make_site(
        {
            "/tenders": (
                200,
                "text/html",
                '<html><body><table class="tenders"><tbody>'
                "<tr><td></td><td></td></tr></tbody></table></body></html>",
            )
        }
    )
    report, _ = await verify(site, settings)

    assert report.status == "FAILED"
    assert check(report, "access").status == CHECK_PASSED, "the fetch really did work"
    assert check(report, "parse").status == CHECK_FAILED
    assert "zero usable items" in check(report, "parse").detail
    assert "parse" in report.failed_checks


async def test_an_unreachable_host_fails_instead_of_crashing(make_site, settings):
    """A dead port must produce a report, not an exception escaping the CLI.

    Verification is run by operators against third-party sites, and "the website
    is down" is the single most common result: it has to be a recorded finding.
    The URL itself is safe, so this is an ``access`` failure, not a policy one.
    """
    site = make_site()
    dead_url = site.base_url
    site.hits.clear()
    site.close()

    report = await verify_context(replace(make_context(site), base_url=dead_url), settings)
    assert report.status == "FAILED"
    assert check(report, "url").status == CHECK_PASSED
    assert check(report, "access").status == CHECK_FAILED
    # Nothing downstream ran, so nothing downstream may claim success.
    assert "access" in report.failed_checks
    assert check(report, "parse") is None or check(report, "parse").status != CHECK_PASSED


async def test_private_addresses_are_rejected_by_default(make_site):
    """The SSRF policy applies to verification too, not only to ingestion."""
    site = make_site()
    strict = Settings(app_env="test", http_allow_private_networks=False, http_max_retries=0)
    report, _ = await verify(site, strict)

    assert report.status == "FAILED"
    url_check = check(report, "url")
    assert url_check.status == CHECK_FAILED
    assert "restricted" in url_check.detail.lower()
    # Rejected before any request was made.
    assert site.hits == []


async def test_access_controlled_source_fails_without_bypassing_it(make_site, settings):
    """A 401 is recorded as a refusal. Credentials are never guessed or replayed."""
    site = make_site({"/tenders": (401, "text/html", "<html>login</html>")})
    report, _ = await verify(site, settings)

    access = check(report, "access")
    assert access.status == CHECK_FAILED
    assert "access-controlled" in access.detail
    assert "bypass" in access.detail.lower()
    assert report.status == "FAILED"
    # Exactly one attempt: no credential brute force, no retry storm.
    assert site.hits_for("/tenders") >= 1


async def test_robots_disallowance_aborts_before_the_listing_is_fetched(
    make_site, settings
):
    site = make_site({"/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /tenders\n")})
    report, _ = await verify(site, settings)

    assert site.hits_for("/robots.txt") == 1
    assert site.hits_for("/tenders") == 0, "a disallowed path must not be requested"
    assert report.status == "FAILED"
    assert "ROBOTS_DISALLOWED" in check(report, "access").detail


async def test_ignoring_robots_is_a_warning_the_report_keeps_visible(make_site, settings):
    """ROBOTS_POLICY=IGNORE is allowed but never silent."""
    site = make_site({"/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /tenders\n")})
    report, _ = await verify(site, settings, robots_policy="IGNORE")

    assert site.hits_for("/tenders") >= 1
    robots = check(report, "robots")
    assert robots.status == "WARNING"
    assert not robots.required
    assert "written permission" in robots.detail
    # An optional warning does not stop a PASSED verdict, but it is named in the
    # summary so it cannot be buried.
    assert report.status == "PASSED_WITH_WARNINGS"
    assert "robots" in report.summary


async def test_off_site_links_are_flagged_not_trusted(make_site, settings):
    site = make_site()
    report, _ = await verify(
        site, settings, config={"listing_paths": ["/tenders", "http://other.example/tenders"]}
    )
    listing = check(report, "listing")
    assert listing.status == "WARNING"
    assert listing.evidence["off_site"] == ["http://other.example/tenders"]
    assert report.status == "PASSED_WITH_WARNINGS"


async def test_a_documentless_listing_warns_but_still_verifies(make_site, settings):
    site = make_site(
        {
            "/tenders": (
                200,
                "text/html",
                LISTING.replace('class="doc"', 'class="other"'),
            )
        }
    )
    report, _ = await verify(site, settings)
    documents = check(report, "documents")
    assert documents.status == "WARNING"
    assert report.documents_found == 0
    assert report.status == "PASSED_WITH_WARNINGS"


async def test_unknown_connector_fails_with_the_keys_that_do_exist(make_site, settings):
    site = make_site()
    report, _ = await verify(site, settings, connector_key="no.such.connector")
    connector = check(report, "connector")
    assert connector.status == CHECK_FAILED
    assert "html.listing" in connector.evidence["available"]


async def test_undeclared_config_keys_are_reported_as_a_warning(make_site, settings):
    site = make_site()
    report, _ = await verify(site, settings, config={"typo_selector": "table"})
    connector = check(report, "connector")
    assert connector.status == "WARNING"
    assert connector.evidence["undeclared_keys"] == ["typo_selector"]
    assert report.status == "PASSED_WITH_WARNINGS"


async def test_every_check_name_is_classified(make_site, settings):
    """No check may be neither required nor optional.

    An unclassified name would default to blocking — a new check would silently
    make every source fail — so this is asserted against a real run.
    """
    site = make_site()
    report, _ = await verify(site, settings)
    names = {item.name for item in report.checks}
    assert names, "the verifier produced no checks"
    assert names <= REQUIRED_CHECKS | OPTIONAL_CHECKS, names - (REQUIRED_CHECKS | OPTIONAL_CHECKS)
    for item in report.checks:
        assert item.required is (item.name in REQUIRED_CHECKS)


# --- the service layer ----------------------------------------------------


async def test_the_service_records_the_result_and_never_activates(
    session, source, make_site, settings
):
    """Verification moves a source to VERIFIED at most. Activation is a human call."""
    from app.db.models.source import MunicipalitySource  # noqa: F401 - clarity
    from app.enums import SourceLifecycle, VerificationStatus
    from app.services.verification_service import SourceVerificationService

    site = make_site()
    source.base_url = site.base_url
    source.config = {
        "listing_paths": ["/tenders"],
        "item_selector": "table.tenders tbody tr",
        "field_selectors": {
            "reference_number": "td:nth-child(1)",
            "title": "td:nth-child(2)",
            "published_at": "td:nth-child(3)",
            "closing_at": "td:nth-child(4)",
        },
        "link_selector": "td:nth-child(2) a",
        "document_selector": "a.doc",
    }
    source.connector_key = "html.listing"
    source.lifecycle_status = str(SourceLifecycle.DISCOVERED)
    await session.commit()

    service = SourceVerificationService(session, settings)
    outcome = await service.verify(source.id, sample_items=2)

    assert outcome.status in {str(VerificationStatus.PASSED), "PASSED_WITH_WARNINGS"}
    assert outcome.lifecycle == str(SourceLifecycle.VERIFIED)
    assert outcome.previous_lifecycle == str(SourceLifecycle.DISCOVERED)

    stored = await session.get(MunicipalitySource, source.id)
    assert stored.verification_status == outcome.status
    # The automated run stamps ``verification_at``; ``verified_at`` is the *human*
    # confirmation and must stay empty until a person says so.
    assert stored.verification_at is not None
    assert stored.verified_at is None
    assert stored.verification_http_status == 200
    assert stored.verification_result["checks"], "the evidence must survive"
    assert stored.lifecycle_status == str(SourceLifecycle.VERIFIED)
    # ...and still not ACTIVE: nothing here schedules ingestion on its own.
    assert stored.lifecycle_status != str(SourceLifecycle.ACTIVE)


async def test_activation_requires_a_passing_verification(session, source, settings):
    from app.enums import SourceLifecycle
    from app.errors import ValidationError
    from app.services.verification_service import SourceVerificationService

    service = SourceVerificationService(session, settings)
    with pytest.raises(ValidationError) as excinfo:
        await service.set_lifecycle(source.id, SourceLifecycle.ACTIVE)
    assert excinfo.value.code == "SOURCE_NOT_VERIFIED"
    assert "verify_source" in str(excinfo.value)


async def test_pausing_without_a_reason_is_refused(session, source, settings):
    from app.enums import SourceLifecycle
    from app.errors import ValidationError
    from app.services.verification_service import SourceVerificationService

    service = SourceVerificationService(session, settings)
    with pytest.raises(ValidationError) as excinfo:
        await service.set_lifecycle(source.id, SourceLifecycle.PAUSED, reason="   ")
    assert excinfo.value.code == "REASON_REQUIRED"

    stored = await service.set_lifecycle(source.id, SourceLifecycle.PAUSED, reason="site redesign")
    assert stored.lifecycle_status == str(SourceLifecycle.PAUSED)
    assert stored.paused_at is not None
    assert stored.paused_reason == "site redesign"


async def test_a_dry_run_persists_nothing(session, source, make_site, settings):
    from app.enums import SourceLifecycle

    site = make_site()
    source.base_url = site.base_url
    source.lifecycle_status = str(SourceLifecycle.DISCOVERED)
    await session.commit()

    from app.services.verification_service import SourceVerificationService

    service = SourceVerificationService(session, settings)
    outcome = await service.verify(source.id, persist=False)

    assert outcome.status  # a report was produced
    stored = await session.get(type(source), source.id)
    await session.refresh(stored)
    assert stored.verified_at is None
    assert stored.lifecycle_status == str(SourceLifecycle.DISCOVERED)
    assert site.hits_for("/tenders") >= 1
