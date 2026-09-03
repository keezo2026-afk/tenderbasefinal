"""URL validation, canonicalisation and SSRF protection.

Outbound fetching is a security boundary: TenderBase follows links published by
third-party websites, so every URL is validated before a request is made.

Policy:

* only ``http`` / ``https``
* no credentials in the URL
* no non-standard ports (unless explicitly allowed)
* DNS resolution must not land on loopback/private/link-local/reserved ranges
  (unless ``allow_private_networks`` is enabled — tests only)
* redirects are re-validated at every hop by the fetcher
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import ParseResult, urljoin, urlparse, urlsplit, urlunparse, urlunsplit

from app.errors import UnsafeURLError

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})

#: Query parameters that carry no meaning for content identity.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "_ga",
    }
)


@dataclass(frozen=True, slots=True)
class URLCheck:
    """Outcome of validating a URL."""

    url: str
    ok: bool
    reason: str | None = None
    host: str | None = None
    resolved_ips: tuple[str, ...] = ()


def is_http_url(value: str | None) -> bool:
    """Cheap syntactic check: is this an absolute http(s) URL?"""
    if not value:
        return False
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme in ALLOWED_SCHEMES and bool(parsed.netloc)


def normalize_url(value: str, *, base: str | None = None, strip_tracking: bool = True) -> str:
    """Canonicalise a URL: resolve relative, lowercase host, drop tracking params."""
    raw = (value or "").strip()
    if not raw:
        raise UnsafeURLError("Empty URL")
    if base:
        raw = urljoin(base, raw)

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    netloc = parts.netloc

    if "@" in netloc:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")

    host = parts.hostname or ""
    port = parts.port
    netloc = host.lower()
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"

    path = parts.path or "/"
    query = parts.query
    if strip_tracking and query:
        kept = [
            pair
            for pair in query.split("&")
            if pair and pair.split("=", 1)[0].lower() not in TRACKING_PARAMS
        ]
        query = "&".join(kept)

    # The fragment never affects server-side content.
    return urlunsplit((scheme, netloc, path, query, ""))


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_host(host: str) -> tuple[str, ...]:
    """Resolve a hostname to its IP addresses (empty tuple when unresolvable)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return ()
    # getaddrinfo's address tuple is heterogeneous as far as the type system goes;
    # for the TCP query above the first element is always the address string.
    return tuple({str(info[4][0]) for info in infos})


def validate_url(
    value: str,
    *,
    allow_private_networks: bool = False,
    check_dns: bool = True,
    allowed_ports: frozenset[int] = ALLOWED_PORTS,
) -> URLCheck:
    """Validate a URL against the outbound-fetch security policy."""
    try:
        candidate = normalize_url(value)
    except UnsafeURLError as exc:
        return URLCheck(value, False, str(exc))

    parts = urlsplit(candidate)
    if parts.scheme not in ALLOWED_SCHEMES:
        return URLCheck(candidate, False, f"scheme '{parts.scheme}' is not allowed")

    host = parts.hostname
    if not host:
        return URLCheck(candidate, False, "missing host")
    if parts.port is not None and parts.port not in allowed_ports:
        return URLCheck(candidate, False, f"port {parts.port} is not allowed")

    if allow_private_networks:
        return URLCheck(candidate, True, None, host, ())

    # A literal IP host is checked directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_disallowed_ip(literal):
            return URLCheck(candidate, False, "destination IP is in a restricted range", host)
        return URLCheck(candidate, True, None, host, (str(literal),))

    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(".local"):
        return URLCheck(candidate, False, "loopback hostnames are not allowed", host)

    if not check_dns:
        return URLCheck(candidate, True, None, host, ())

    ips = resolve_host(host)
    if not ips:
        return URLCheck(candidate, False, "host could not be resolved", host)
    for raw_ip in ips:
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return URLCheck(candidate, False, "unparseable resolved address", host, ips)
        if _is_disallowed_ip(ip):
            return URLCheck(candidate, False, "host resolves to a restricted range", host, ips)
    return URLCheck(candidate, True, None, host, ips)


def require_safe_url(value: str, *, allow_private_networks: bool = False) -> str:
    """Validate and return a URL, raising :class:`UnsafeURLError` when rejected."""
    check = validate_url(value, allow_private_networks=allow_private_networks)
    if not check.ok:
        raise UnsafeURLError(f"Rejected URL: {check.reason}", details={"url": value})
    return check.url


def same_registrable_host(a: str, b: str) -> bool:
    """True when two URLs share the same host (ignoring a leading ``www.``)."""

    def host_of(url: str) -> str:
        h = (urlsplit(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h

    return host_of(a) == host_of(b) and host_of(a) != ""


def filename_from_url(url: str) -> str | None:
    """Best-effort filename from a URL path (never trusted as identity)."""
    path = urlsplit(url).path
    name = path.rsplit("/", 1)[-1].strip()
    return name or None


def with_query(url: str, **params: str | int) -> str:
    """Return ``url`` with additional query parameters appended."""
    parts: ParseResult = urlparse(url)
    existing = [pair for pair in parts.query.split("&") if pair]
    existing.extend(f"{key}={value}" for key, value in params.items())
    return urlunparse(parts._replace(query="&".join(existing)))
