"""SSRF guard for webhook URLs: what is accepted, what is refused, and that delivery goes to the checked address."""
from __future__ import annotations

import datetime
import http.server
import ipaddress
import socketserver
import ssl
import threading
from functools import partial
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from vreview import webhook_guard, webhooks
from vreview.config import settings
from vreview.webhook_guard import (
    WebhookResolutionError,
    WebhookURLError,
    is_public_address,
    resolve_webhook_target,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


class _FakeDNS:
    """Name resolution table. `dns[host] = [addr, ...]` answers every lookup the same way;
    `dns[host] = [[addr, ...], [addr, ...]]` gives one answer per lookup (the last one repeats)."""

    def __init__(self) -> None:
        self.answers: dict[str, list] = {}
        self.lookups: list[str] = []

    def __setitem__(self, host: str, answer: list) -> None:
        self.answers[host] = answer

    def __call__(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        answer = self.answers.get(host)
        if answer is None:
            raise WebhookResolutionError("webhook_url host could not be resolved")
        if answer and isinstance(answer[0], list):
            answer = answer[min(self.lookups.count(host) - 1, len(answer) - 1)]
        return list(answer)


@pytest.fixture
def dns(monkeypatch) -> _FakeDNS:
    fake = _FakeDNS()
    monkeypatch.setattr(webhook_guard, "_resolve_host", fake)
    return fake


# --- address classes ----------------------------------------------------------------------------------------------

REJECTED_ADDRESSES = {
    "private v4": ["10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255", "192.168.0.1", "192.168.255.254"],
    "loopback": ["127.0.0.1", "127.1.2.3", "::1"],
    "link-local": ["169.254.169.254", "169.254.0.1", "fe80::1", "febf::1"],
    "multicast": ["224.0.0.1", "239.255.255.250", "ff02::1", "ff0e::1"],
    "unspecified": ["0.0.0.0", "0.1.2.3", "::"],
    "reserved": ["240.0.0.1", "255.255.255.255", "192.0.0.1"],
    "documentation and benchmarking": ["192.0.2.1", "198.51.100.7", "203.0.113.9", "198.18.0.1", "2001:db8::1"],
    "shared address space": ["100.64.0.1", "100.100.100.200", "100.127.255.255"],
    "private v6": ["fc00::1", "fd00:ec2::254", "fdff::1", "fec0::1"],
    "ipv4-mapped v6": [
        "::ffff:8.8.8.8",  # even a public v4 behind a mapped address is refused: nothing legitimate resolves to one
        "::ffff:127.0.0.1",
        "::ffff:7f00:1",
        "::ffff:10.0.0.1",
        "::ffff:192.168.1.1",
        "::ffff:169.254.169.254",
        "::ffff:0.0.0.0",
    ],
    "ipv4-compatible v6": ["::127.0.0.1", "::10.0.0.1", "::1.2.3.4"],
    "nat64 of a private v4": ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b::a00:1"],
    "6to4 of a private v4": ["2002:7f00:1::", "2002:a9fe:a9fe::1", "2002:c0a8:101::"],
    "outside global unicast v6": ["100::1", "4000::1", "8000::1", "e000::1"],
}


@pytest.mark.parametrize(
    "address",
    [pytest.param(a, id=f"{group}:{a}") for group, addrs in REJECTED_ADDRESSES.items() for a in addrs],
)
def test_non_public_addresses_are_rejected(address):
    assert is_public_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize(
    "address", [PUBLIC_V4, "1.1.1.1", "8.8.8.8", "151.101.1.69", PUBLIC_V6, "2a00:1450:4001:81b::200e"]
)
def test_public_addresses_are_accepted(address):
    assert is_public_address(ipaddress.ip_address(address)) is True


def _bracket(address: str) -> str:
    return f"[{address}]" if ":" in address else address


@pytest.mark.parametrize(
    "address",
    [pytest.param(a, id=f"{group}:{a}") for group, addrs in REJECTED_ADDRESSES.items() for a in addrs],
)
def test_url_with_a_literal_non_public_address_is_rejected(address):
    with pytest.raises(WebhookURLError, match="public host"):
        resolve_webhook_target(f"https://{_bracket(address)}/hook")


@pytest.mark.parametrize(
    "host",
    [
        "2130706433",  # 127.0.0.1 as one decimal number
        "0x7f000001",  # hex
        "0177.0.0.1",  # octal
        "127.1",  # short form
        "127.0.1",
        "0",  # 0.0.0.0
        "localhost",
        "localhost.",
    ],
)
def test_numeric_and_name_forms_of_loopback_are_rejected(host):
    with pytest.raises(WebhookURLError):
        resolve_webhook_target(f"https://{host}/hook")


def test_zone_id_on_a_link_local_address_is_rejected():
    with pytest.raises(WebhookURLError, match="public host"):
        resolve_webhook_target("https://[fe80::1%25eth0]/hook")


@pytest.mark.parametrize("url", ["https://evil.com\\@127.0.0.1/", "https://127.0.0.1\\@evil.com/"])
def test_backslash_userinfo_urls_are_rejected(url):
    # urllib and httpx read these differently. The guard parses with httpx, the library that sends the request.
    with pytest.raises(WebhookURLError):
        resolve_webhook_target(url)


def test_fragment_trick_targets_the_real_host_only(dns):
    dns["evil.com"] = [PUBLIC_V4]
    target = resolve_webhook_target("https://evil.com#@127.0.0.1/")
    assert (target.host, target.addresses, target.path) == ("evil.com", (PUBLIC_V4,), "/")


# --- names --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "answer",
    [
        ["10.1.2.3"],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["fd00::5"],
        ["::ffff:127.0.0.1"],
        [PUBLIC_V4, "10.0.0.5"],  # one private answer among public ones is enough to refuse
        ["192.168.1.1", PUBLIC_V4],
        [PUBLIC_V4, PUBLIC_V6, "::1"],
    ],
)
def test_name_resolving_to_a_non_public_address_is_rejected(dns, answer):
    dns["hooks.example.com"] = answer
    with pytest.raises(WebhookURLError, match="public host"):
        resolve_webhook_target("https://hooks.example.com/hook")


def test_internal_service_names_are_rejected_because_they_resolve_to_private_addresses(dns):
    for name in ("postgres", "minio", "hatchet", "api"):
        dns[name] = ["172.18.0.5"]
        with pytest.raises(WebhookURLError, match="public host"):
            resolve_webhook_target(f"https://{name}:9000/")


def test_unresolvable_name_is_rejected(dns):
    with pytest.raises(WebhookResolutionError, match="could not be resolved"):
        resolve_webhook_target("https://nope.example.com/hook")
    assert issubclass(WebhookResolutionError, WebhookURLError)


def test_error_message_does_not_reveal_the_resolved_address(dns):
    dns["leak.example.com"] = ["10.9.8.7"]
    with pytest.raises(WebhookURLError) as exc:
        resolve_webhook_target("https://leak.example.com/")
    assert "10.9.8.7" not in str(exc.value)


# --- scheme and shape ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        f"http://{PUBLIC_V4}/hook",
        "ftp://example.com/hook",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "//example.com/hook",
        "example.com/hook",
        "",
        "https://",
        "https:///hook",
        f"https://user:secret@{PUBLIC_V4}/hook",
        f"https://{PUBLIC_V4}/has space",
        f"https://{PUBLIC_V4}/new\nline",
        "https://" + "a" * 1100 + ".com/",
    ],
)
def test_malformed_or_non_https_urls_are_rejected(url):
    with pytest.raises(WebhookURLError):
        resolve_webhook_target(url)


def test_http_is_accepted_only_with_the_flag_and_the_address_check_still_applies(monkeypatch):
    with pytest.raises(WebhookURLError, match="https"):
        resolve_webhook_target(f"http://{PUBLIC_V4}/hook")

    monkeypatch.setenv("VREVIEW_WEBHOOK_ALLOW_HTTP", "1")
    settings.cache_clear()
    assert resolve_webhook_target(f"http://{PUBLIC_V4}/hook").scheme == "http"
    with pytest.raises(WebhookURLError, match="public host"):
        resolve_webhook_target("http://127.0.0.1/hook")
    with pytest.raises(WebhookURLError, match="public host"):
        resolve_webhook_target("http://169.254.169.254/latest/meta-data")
    with pytest.raises(WebhookURLError):
        resolve_webhook_target("ftp://example.com/")


# --- accepted targets ---------------------------------------------------------------------------------------------

def test_public_literal_and_name_targets(dns):
    t = resolve_webhook_target(f"https://{PUBLIC_V4}/hook?a=1&b=2")
    assert (t.scheme, t.host, t.port, t.path, t.addresses) == ("https", PUBLIC_V4, 443, "/hook?a=1&b=2", (PUBLIC_V4,))
    assert t.host_header == PUBLIC_V4
    assert t.request_url(PUBLIC_V4) == f"https://{PUBLIC_V4}:443/hook?a=1&b=2"

    t6 = resolve_webhook_target(f"https://[{PUBLIC_V6}]:8443/x")
    assert t6.addresses == (PUBLIC_V6,)
    assert t6.host_header == f"[{PUBLIC_V6}]:8443"
    assert t6.request_url(PUBLIC_V6) == f"https://[{PUBLIC_V6}]:8443/x"

    dns["Hooks.Example.COM"] = [PUBLIC_V4]
    dns["hooks.example.com"] = [PUBLIC_V4, PUBLIC_V6]
    named = resolve_webhook_target("https://Hooks.Example.COM:8443")
    assert named.host == "hooks.example.com"
    assert named.path == "/"
    assert named.host_header == "hooks.example.com:8443"
    assert named.addresses == (PUBLIC_V4, PUBLIC_V6)
    assert named.request_url(PUBLIC_V6) == f"https://[{PUBLIC_V6}]:8443/"


def test_request_url_refuses_an_address_that_was_not_validated():
    t = resolve_webhook_target(f"https://{PUBLIC_V4}/hook")
    with pytest.raises(ValueError):
        t.request_url("127.0.0.1")


def test_internationalised_host_is_resolved_in_ascii(dns):
    dns["xn--bcher-kva.example"] = [PUBLIC_V4]
    assert resolve_webhook_target("https://bücher.example/").host == "xn--bcher-kva.example"


# --- sending: the validated address is the address contacted -----------------------------------------------------

class _Receiver:
    """HTTP server on 127.0.0.1 that records requests and answers with a configurable status."""

    def __init__(self, status: int = 200, location: str | None = None, tls: ssl.SSLContext | None = None):
        outer = self
        self.requests: list[dict] = []
        self.sni: list[str | None] = []
        self.status = status
        self.location = location

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "host": self.headers.get("Host"), "body": self.rfile.read(length)})
                self.send_response(outer.status)
                if outer.location:
                    self.send_header("Location", outer.location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        if tls is not None:
            def record_sni(sock, server_name, ctx):
                outer.sni.append(server_name)

            tls.sni_callback = record_sni
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver():
    made: list[_Receiver] = []

    def make(**kwargs) -> _Receiver:
        r = _Receiver(**kwargs)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def test_delivery_connects_to_the_validated_address_and_keeps_the_host_header(allow_loopback_webhooks, dns, receiver):
    rx = receiver()
    dns["hooks.test"] = ["127.0.0.1"]  # "hooks.test" does not exist in real DNS, so only the pinned address can work

    status = webhooks._post_once(f"http://hooks.test:{rx.port}/path?x=1", b'{"a":1}', {"Content-Type": "application/json"})

    assert status == 200
    assert rx.requests == [{"path": "/path?x=1", "host": f"hooks.test:{rx.port}", "body": b'{"a":1}'}]


def test_delivery_resolves_once_per_attempt_and_uses_that_answer(allow_loopback_webhooks, dns, receiver):
    rx = receiver()
    dns["hooks.test"] = ["127.0.0.1"]
    webhooks._post_once(f"http://hooks.test:{rx.port}/", b"{}", {})
    assert dns.lookups == ["hooks.test"]


def test_dns_rebinding_between_creation_and_delivery_is_caught(monkeypatch, dns, receiver):
    monkeypatch.setenv("VREVIEW_WEBHOOK_ALLOW_HTTP", "1")
    settings.cache_clear()
    rx = receiver()  # listening on 127.0.0.1, which the guard must not let anything reach
    dns["rebind.test"] = [[PUBLIC_V4], ["127.0.0.1"]]

    assert resolve_webhook_target(f"http://rebind.test:{rx.port}/").addresses == (PUBLIC_V4,)  # at creation: public
    with pytest.raises(WebhookURLError, match="public host"):  # at delivery: now loopback
        webhooks._post_once(f"http://rebind.test:{rx.port}/", b"{}", {})
    assert rx.requests == []


def test_a_private_target_is_never_contacted(monkeypatch, receiver):
    monkeypatch.setenv("VREVIEW_WEBHOOK_ALLOW_HTTP", "1")
    settings.cache_clear()
    rx = receiver()
    with pytest.raises(WebhookURLError):
        webhooks._post_once(f"http://127.0.0.1:{rx.port}/", b"{}", {})
    assert rx.requests == []


def test_redirects_are_not_followed(allow_loopback_webhooks, receiver):
    second = receiver()
    first = receiver(status=302, location=f"http://127.0.0.1:{second.port}/internal")

    assert webhooks._post_once(f"http://127.0.0.1:{first.port}/hook", b"{}", {}) == 302

    assert len(first.requests) == 1
    assert second.requests == []


def test_next_validated_address_is_tried_when_the_first_refuses_the_connection(allow_loopback_webhooks, dns, receiver):
    rx = receiver()
    dns["hooks.test"] = ["127.0.0.2", "127.0.0.1"]  # nothing listens on 127.0.0.2
    assert webhooks._post_once(f"http://hooks.test:{rx.port}/", b"{}", {}) == 200
    assert len(rx.requests) == 1


# TLS: the socket goes to the pinned IP, but SNI and certificate checks use the original host name.

def _self_signed(hostname: str, directory: Path) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path, key_path = directory / f"{hostname}.crt", directory / f"{hostname}.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return cert_path, key_path, cert_pem.decode()


def _tls_receiver(receiver, tmp_path: Path, cert_hostname: str):
    cert_path, key_path, cert_pem = _self_signed(cert_hostname, tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)
    client_ctx = ssl.create_default_context(cadata=cert_pem)  # trusts only the test certificate
    return receiver(tls=server_ctx), client_ctx


def test_https_delivery_sends_the_host_name_as_sni_and_verifies_the_certificate_against_it(
    allow_loopback_webhooks, dns, receiver, tmp_path, monkeypatch
):
    rx, client_ctx = _tls_receiver(receiver, tmp_path, "hooks.test")
    monkeypatch.setattr(webhooks.httpx, "Client", partial(httpx.Client, verify=client_ctx))
    dns["hooks.test"] = ["127.0.0.1"]

    assert webhooks._post_once(f"https://hooks.test:{rx.port}/hook", b"{}", {}) == 200

    assert rx.sni == ["hooks.test"]
    assert rx.requests[0]["host"] == f"hooks.test:{rx.port}"


def test_https_delivery_fails_when_the_certificate_is_for_another_host(
    allow_loopback_webhooks, dns, receiver, tmp_path, monkeypatch
):
    rx, client_ctx = _tls_receiver(receiver, tmp_path, "other.test")
    monkeypatch.setattr(webhooks.httpx, "Client", partial(httpx.Client, verify=client_ctx))
    dns["hooks.test"] = ["127.0.0.1"]

    with pytest.raises(httpx.TransportError):
        webhooks._post_once(f"https://hooks.test:{rx.port}/hook", b"{}", {})
    assert rx.requests == []
