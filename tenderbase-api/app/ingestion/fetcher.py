"""Responsible HTTP fetching.

Implements the politeness and safety contract every connector relies on:

* timeouts, retries, exponential backoff with jitter
* per-host token-bucket rate limiting
* robots.txt awareness (cached per host)
* SSRF protection (scheme/port/IP-range validation on every hop)
* streaming download with a hard response-size ceiling
* content-type validation and redirect limits
* structured logging of every request
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.config import Settings, get_settings
from app.connectors.base import DiscoveryTarget, FetchResult, SourceContext
from app.errors import (
    PermanentFetchError,
    ResponseTooLargeError,
    RetryableFetchError,
    RobotsDisallowedError,
    UnsafeURLError,
)
from app.logging import get_logger
from app.utils.urls import validate_url

logger = get_logger("tenderbase.fetcher")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 509, 522, 524})


class RateLimiter:
    """Simple per-host token bucket, shared by all connectors in a process."""

    def __init__(self) -> None:
        self._next_allowed: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, host: str, per_minute: int) -> None:
        """Block until this host may be contacted again."""
        if per_minute <= 0:
            return
        interval = 60.0 / per_minute
        async with self._locks[host]:
            now = time.monotonic()
            wait = self._next_allowed[host] - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            # Add jitter so many sources do not synchronise into bursts.
            self._next_allowed[host] = now + interval * random.uniform(0.9, 1.15)


class RobotsCache:
    """Caches robots.txt decisions per host for the process lifetime."""

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._cache: dict[str, tuple[float, RobotFileParser | None]] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    async def allowed(self, client: httpx.AsyncClient, url: str, user_agent: str) -> bool:
        """True when ``url`` may be fetched according to the host's robots.txt.

        A missing or unreachable robots.txt is treated as *allowed* (RFC 9309),
        a malformed one as allowed, and an explicit ``Disallow`` as denied.
        """
        parts = urlsplit(url)
        host_key = f"{parts.scheme}://{parts.netloc}"
        async with self._lock:
            cached = self._cache.get(host_key)
            fresh = cached is not None and (time.monotonic() - cached[0]) < self._ttl
            if not fresh:
                parser = await self._load(client, host_key, user_agent)
                self._cache[host_key] = (time.monotonic(), parser)
            else:
                parser = cached[1]  # type: ignore[index]
        if parser is None:
            return True
        return parser.can_fetch(user_agent, url)

    async def _load(
        self, client: httpx.AsyncClient, host_key: str, user_agent: str
    ) -> RobotFileParser | None:
        robots_url = f"{host_key}/robots.txt"
        try:
            response = await client.get(robots_url, timeout=10.0)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        parser = RobotFileParser()
        try:
            parser.parse(response.text.splitlines())
        except Exception:  # noqa: BLE001 - malformed robots.txt must not break ingestion
            return None
        return parser


@dataclass(slots=True)
class FetchPolicy:
    """Per-fetch overrides of the global HTTP policy."""

    timeout: float | None = None
    max_retries: int | None = None
    max_bytes: int | None = None
    allowed_content_types: tuple[str, ...] | None = None
    respect_robots: bool | None = None
    rate_limit_per_minute: int | None = None


class HTTPFetcher:
    """Async HTTP client wrapper enforcing the fetch policy.

    Use as an async context manager, or pass an existing ``httpx.AsyncClient``
    (tests inject a client backed by ``httpx.MockTransport``).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        robots_cache: RobotsCache | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._rate_limiter = rate_limiter or RateLimiter()
        self._robots = robots_cache or RobotsCache()

    # -- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> HTTPFetcher:
        self.client  # noqa: B018 - force client creation
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.http_timeout_seconds),
                follow_redirects=True,
                max_redirects=self.settings.http_max_redirects,
                headers=self.default_headers(),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def default_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.http_user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-ZA,en;q=0.9",
        }

    # -- core -------------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        source: SourceContext | None = None,
        target: DiscoveryTarget | None = None,
        policy: FetchPolicy | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> FetchResult:
        """Fetch a URL under the full safety/politeness policy."""
        policy = policy or FetchPolicy()
        cfg = self.settings

        check = validate_url(url, allow_private_networks=cfg.http_allow_private_networks)
        if not check.ok:
            raise UnsafeURLError(f"Rejected URL: {check.reason}", details={"url": url})
        safe_url = check.url
        host = check.host or urlsplit(safe_url).netloc

        respect_robots = (
            policy.respect_robots
            if policy.respect_robots is not None
            else cfg.http_respect_robots and (source is None or source.robots_policy == "RESPECT")
        )
        if respect_robots and not await self._robots.allowed(
            self.client, safe_url, cfg.http_user_agent
        ):
            raise RobotsDisallowedError("robots.txt disallows this URL", details={"url": safe_url})

        per_minute = (
            policy.rate_limit_per_minute
            or (source.rate_limit_per_minute if source else None)
            or cfg.http_default_rate_limit_per_minute
        )
        max_retries = policy.max_retries if policy.max_retries is not None else cfg.http_max_retries
        max_bytes = policy.max_bytes or cfg.http_max_response_bytes
        timeout = policy.timeout or cfg.http_timeout_seconds

        request_headers = {**self.default_headers(), **(headers or {})}
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            await self._rate_limiter.acquire(host, per_minute)
            started = time.perf_counter()
            try:
                result = await self._attempt(
                    method=method,
                    url=safe_url,
                    headers=request_headers,
                    params=params,
                    timeout=timeout,
                    max_bytes=max_bytes,
                    allowed_content_types=policy.allowed_content_types,
                    target=target,
                )
            except (RetryableFetchError, httpx.HTTPError) as exc:
                last_error = exc
                logger.warning(
                    "fetch.retryable_failure",
                    url=safe_url,
                    attempt=attempt + 1,
                    max_attempts=max_retries + 1,
                    error=str(exc),
                    source_id=source.id if source else None,
                )
                if attempt >= max_retries:
                    break
                await asyncio.sleep(self._backoff(attempt))
                continue
            except (PermanentFetchError, ResponseTooLargeError, UnsafeURLError):
                raise

            result.elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "fetch.ok",
                url=safe_url,
                status=result.status_code,
                bytes=result.size,
                duration=round(result.elapsed_ms, 2),
                content_type=result.content_type,
                source_id=source.id if source else None,
            )
            return result

        raise RetryableFetchError(
            f"Failed to fetch {safe_url} after {max_retries + 1} attempts: {last_error}",
            details={"url": safe_url},
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at 30s."""
        base = self.settings.http_backoff_base_seconds * (2**attempt)
        return min(30.0, random.uniform(0, base))

    async def _attempt(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        timeout: float,
        max_bytes: int,
        allowed_content_types: tuple[str, ...] | None,
        target: DiscoveryTarget | None,
    ) -> FetchResult:
        request = self.client.build_request(
            method, url, headers=headers, params=params, timeout=timeout
        )
        response = await self.client.send(request, stream=True)
        try:
            # Re-validate the final URL: redirects may point somewhere unsafe.
            final_check = validate_url(
                str(response.url),
                allow_private_networks=self.settings.http_allow_private_networks,
            )
            if not final_check.ok:
                raise UnsafeURLError(
                    f"Redirect target rejected: {final_check.reason}",
                    details={"url": str(response.url)},
                )

            status = response.status_code
            if status in RETRYABLE_STATUS:
                raise RetryableFetchError(f"HTTP {status}", details={"url": url, "status": status})
            if status >= 400:
                raise PermanentFetchError(f"HTTP {status}", details={"url": url, "status": status})

            content_type = (
                (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            )
            if allowed_content_types and content_type and content_type not in allowed_content_types:
                raise PermanentFetchError(
                    f"Unexpected content-type '{content_type}'",
                    details={"url": url, "expected": list(allowed_content_types)},
                )

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResponseTooLargeError(
                    f"Content-Length {declared} exceeds limit {max_bytes}",
                    details={"url": url},
                )

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ResponseTooLargeError(
                        f"Response exceeded {max_bytes} bytes", details={"url": url}
                    )
                chunks.append(chunk)

            return FetchResult(
                target=target or DiscoveryTarget(url=url),
                url=str(response.url),
                status_code=status,
                content=b"".join(chunks),
                headers={k.lower(): v for k, v in response.headers.items()},
                encoding=response.charset_encoding or "utf-8",
            )
        finally:
            await response.aclose()

    async def stream_to(
        self,
        url: str,
        *,
        sink: Any,
        max_bytes: int,
        allowed_content_types: tuple[str, ...] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Stream a (potentially large) file into ``sink`` without buffering it.

        Returns response metadata; the caller hashes and stores the bytes.
        """
        check = validate_url(url, allow_private_networks=self.settings.http_allow_private_networks)
        if not check.ok:
            raise UnsafeURLError(f"Rejected URL: {check.reason}", details={"url": url})

        await self._rate_limiter.acquire(
            check.host or "", self.settings.http_default_rate_limit_per_minute
        )
        request = self.client.build_request(
            "GET",
            check.url,
            headers=self.default_headers(),
            timeout=timeout or self.settings.http_timeout_seconds,
        )
        response = await self.client.send(request, stream=True)
        try:
            if response.status_code >= 400:
                raise PermanentFetchError(
                    f"HTTP {response.status_code}",
                    details={"url": check.url, "status": response.status_code},
                )
            content_type = (
                (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            )
            if allowed_content_types and content_type and content_type not in allowed_content_types:
                raise PermanentFetchError(
                    f"Unexpected content-type '{content_type}'", details={"url": check.url}
                )

            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ResponseTooLargeError(
                        f"Download exceeded {max_bytes} bytes", details={"url": check.url}
                    )
                sink.write(chunk)
            return {
                "url": str(response.url),
                "status_code": response.status_code,
                "content_type": content_type,
                "size": total,
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
                "headers": {k.lower(): v for k, v in response.headers.items()},
            }
        finally:
            await response.aclose()
