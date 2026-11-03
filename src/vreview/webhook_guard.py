"""SSRF guard for user-supplied webhook URLs.

`webhook_url` comes from API callers and the server POSTs to it, so it must not reach the stack's own network
(Postgres, MinIO, Hatchet, the cloud metadata service, the host). Two entry points:

* `resolve_webhook_target(url)` parses the URL, resolves its host and checks every resolved address. It runs at
  `create_review` (400 on failure) and again right before each delivery attempt.
* `WebhookTarget.request_url(address)` / `host_header` let the sender connect to the address that was just
  validated, with the original host name kept for the `Host` header and TLS (SNI and certificate checks). Because
  the sender never resolves the name a second time, DNS rebinding between check and connect is not possible.

Allowed: https, or http when VREVIEW_WEBHOOK_ALLOW_HTTP=1, to hosts whose addresses are all public unicast.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass

import httpx

from vreview.config import settings

logger = logging.getLogger(__name__)

MAX_URL_LENGTH = 1024  # runs.webhook_url is VARCHAR(1024)

_IPv4 = ipaddress.IPv4Address
_IPv6 = ipaddress.IPv6Address
_GLOBAL_UNICAST_V6 = ipaddress.IPv6Network("2000::/3")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


class WebhookURLError(ValueError):
    """The URL is not acceptable as a webhook target. The message is safe to return to the caller."""


class WebhookResolutionError(WebhookURLError):
    """The host did not resolve. Not a policy violation: a later attempt may succeed."""


@dataclass(frozen=True)
class WebhookTarget:
    """A webhook URL whose host resolved to public addresses only."""

    scheme: str
    host: str  # ASCII (IDNA) host name or IP literal, without brackets
    port: int  # effective port
    default_port: bool  # the URL had no explicit port
    path: str  # raw path and query, always starts with "/"
    addresses: tuple[str, ...]  # validated, in connect order

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.default_port else f"{host}:{self.port}"

    def request_url(self, address: str) -> str:
        """URL that connects to `address` (an entry of `addresses`) instead of resolving the host name again."""
        if address not in self.addresses:
            raise ValueError(f"{address} was not validated for this webhook target")
        ip = f"[{address}]" if ":" in address else address
        return f"{self.scheme}://{ip}:{self.port}{self.path}"


def _embedded_ipv4(ip: _IPv6) -> list[_IPv4]:
    """IPv4 addresses a v6 address can route to: IPv4-mapped (::ffff:a.b.c.d), 6to4 and NAT64 (64:ff9b::/96)."""
    found: list[_IPv4] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip in _NAT64:
        found.append(_IPv4(int(ip) & 0xFFFFFFFF))
    return found


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for a globally routable unicast address. Rejects private, loopback, link-local, multicast, reserved,
    unspecified, shared (100.64/10) and documentation ranges, and v6 addresses that embed such a v4 address."""
    if isinstance(ip, _IPv6):
        if any(not is_public_address(v4) for v4 in _embedded_ipv4(ip)):
            return False
        if ip not in _GLOBAL_UNICAST_V6:  # ::/8 (unspecified, loopback, v4-compatible), fc00::/7, fe80::/10, ff00::/8, ...
            return False
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return False
    return ip.is_global


def _parse_address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(text.split("%", 1)[0])  # drop an IPv6 zone id such as fe80::1%eth0


def _resolve_host(host: str, port: int) -> list[str]:
    """All addresses `host` resolves to. A seam for tests."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebhookResolutionError("webhook_url host could not be resolved") from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def resolve_webhook_target(url: str) -> WebhookTarget:
    """Parse `url`, resolve its host, and return the validated target. Raises WebhookURLError.

    Blocking (DNS): call it from a thread inside async code.
    """
    if not isinstance(url, str) or not url:
        raise WebhookURLError("webhook_url must be a non-empty string")
    if len(url) > MAX_URL_LENGTH:
        raise WebhookURLError(f"webhook_url must be at most {MAX_URL_LENGTH} characters")
    if any(ch <= " " or ch == "\x7f" for ch in url):
        raise WebhookURLError("webhook_url must not contain spaces or control characters")

    # Parse with the library that sends the request, so the checked host is the contacted host.
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError) as exc:
        raise WebhookURLError("webhook_url is not a valid URL") from exc

    allow_http = settings().webhook_allow_http
    if parsed.scheme == "https" or (allow_http and parsed.scheme == "http"):
        pass
    else:
        raise WebhookURLError("webhook_url must use https" if not allow_http else "webhook_url must use http or https")
    if parsed.userinfo:
        raise WebhookURLError("webhook_url must not contain credentials")
    host = parsed.raw_host.decode("ascii") if parsed.raw_host else ""
    if not host:
        raise WebhookURLError("webhook_url must include a host")

    default_port = parsed.port is None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        addresses = [str(_parse_address(host))]  # IP literal: nothing to resolve
    except ValueError:
        # Names, and numeric forms like 2130706433 or 0x7f.1 that the resolver turns into IPs.
        addresses = _resolve_host(host, port)
    if not addresses:
        raise WebhookResolutionError("webhook_url host could not be resolved")

    for address in addresses:
        try:
            public = is_public_address(_parse_address(address))
        except ValueError:
            public = False
        if not public:
            logger.warning("Rejected webhook host %r: it resolves to non-public address %s", host, address)
            raise WebhookURLError(
                "webhook_url must point to a public host (private, loopback, link-local, multicast and "
                "reserved addresses are not allowed)"
            )

    return WebhookTarget(
        scheme=parsed.scheme,
        host=host,
        port=port,
        default_port=default_port,
        path=parsed.raw_path.decode("ascii") or "/",
        addresses=tuple(addresses),
    )
