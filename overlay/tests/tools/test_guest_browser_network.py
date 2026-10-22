"""Behavioral contracts for the guest browser's only external network path.

Every network operation is an injected in-memory transport. No private network,
resolver, system proxy or real external service is contacted by this suite.
"""

import socket
import ssl
import threading
import time

import pytest

from tools import guest_browser_network as net


PUBLIC = "93.184.216.34"


class MemorySocket:
    def __init__(self, wire=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"):
        self.wire = bytearray(wire)
        self.sent = b""
        self.targets = []
        self.closed = False
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, target):
        self.targets.append(target)

    def sendall(self, data):
        self.sent += data

    def recv_into(self, buffer):
        count = min(len(buffer), len(self.wire))
        buffer[:count] = self.wire[:count]
        del self.wire[:count]
        return count

    def close(self):
        self.closed = True


def record(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (ip, 80))


@pytest.fixture
def transport(monkeypatch):
    sockets = []
    lookups = []
    wires = []

    def resolve(host, port, family, kind):
        lookups.append((host, port))
        return [record(PUBLIC)]

    def make_socket(family, kind):
        assert family == socket.AF_INET
        assert kind == socket.SOCK_STREAM
        sock = MemorySocket(wires.pop(0)) if wires else MemorySocket()
        sockets.append(sock)
        return sock

    monkeypatch.setattr(net.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(net.socket, "socket", make_socket)
    return sockets, lookups, wires


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/", "http://user:password@example.com/",
    "http://example.com@127.0.0.1/", "http://example.com:81/", "https://example.com:80/",
    "http://example.com:080/", "http://example.com:/", "http://example.com/#@127.0.0.1",
    "http://example.com\\@127.0.0.1/", "http://example.com%00.evil/", "http://[fe80::1%25eth0]/",
    "http://example.com/\r\nHost: localhost", " http://example.com/", "http://metadata.goog/",
    "http://metadata.google.internal/", "http://" + "x" * 8200, "http://a..example/",
])
def test_invalid_urls_never_resolve_or_connect(transport, url):
    sockets, lookups, _ = transport
    with pytest.raises(net.PublicNetworkError):
        net.fetch_public(url)
    assert not sockets and not lookups


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.148.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.100.100.200", "168.63.129.16", "100.64.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255",
    "198.18.0.1", "192.0.2.1", "::1", "::", "fc00::1", "fe80::1", "ff02::1",
    "::ffff:8.8.8.8", "::ffff:127.0.0.1", "64:ff9b::808:808", "64:ff9b:1::1",
    "2002:0808:0808::1", "2001:0000:4136:e378:8000:63bf:3fff:fdd2", "2001:db8::1",
])
@pytest.mark.parametrize("mixed", [False, True])
def test_all_dns_answers_checked_before_any_connect(monkeypatch, transport, address, mixed):
    sockets, _, _ = transport
    answers = ([record(PUBLIC)] if mixed else []) + [record(address)]
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *args: answers)
    with pytest.raises(net.PublicNetworkError):
        net.fetch_public("http://example.com/")
    assert not sockets


def test_pins_numeric_ipv4_no_second_resolution_and_no_proxy(transport, monkeypatch):
    sockets, lookups, _ = transport
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    result = net.fetch_public("http://example.com:80/path?q=1", headers={
        "Authorization": "Bearer secret", "Cookie": "session=secret", "Host": "localhost",
        "Proxy-Authorization": "secret", "Referer": "https://secret/", "Accept-Encoding": "gzip",
        "Accept": "text/html", "X-Secret": "private",
    })
    assert result == {"status": 200, "headers": {}, "body": b"ok",
                      "url": "http://example.com/path?q=1"}
    assert lookups == [("example.com", 80)]
    assert sockets[0].targets == [(PUBLIC, 80)]
    request = sockets[0].sent
    assert b"Host: example.com\r\n" in request
    assert b"Accept-Encoding: identity\r\n" in request
    assert b"accept: text/html\r\n" in request
    assert b"secret" not in request and b"localhost" not in request
    assert sockets[0].closed


def test_hostname_verified_default_tls_over_pinned_connection(transport, monkeypatch):
    sockets, _, _ = transport
    contexts = []
    real_context = ssl.create_default_context()

    class Context:
        def wrap_socket(self, sock, server_hostname):
            assert real_context.check_hostname
            assert real_context.verify_mode == ssl.CERT_REQUIRED
            contexts.append(server_hostname)
            return sock

    monkeypatch.setattr(net.ssl, "create_default_context", lambda: Context())
    assert net.fetch_public("https://example.com/")["body"] == b"ok"
    assert contexts == ["example.com"]
    assert sockets[0].targets == [(PUBLIC, 443)]
    assert b"Host: example.com" in sockets[0].sent


def test_valid_dual_stack_uses_only_ipv4(transport, monkeypatch):
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *args: [
        record("2606:4700:4700::1111"), record(PUBLIC)])
    assert net.fetch_public("http://example.com/")["body"] == b"ok"
    assert transport[0][0].targets == [(PUBLIC, 80)]


def test_ipv6_only_refused_without_socket(transport, monkeypatch):
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *args: [record("2606:4700:4700::1111")])
    with pytest.raises(net.PublicNetworkError, match="IPv4"):
        net.fetch_public("http://example.com/")
    assert not transport[0]


def test_redirect_rechecks_dns_and_blocks_rebinding(monkeypatch, transport):
    sockets, _, wires = transport
    wires.append(b"HTTP/1.1 302 Found\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n")
    calls = []

    def resolver(*args):
        calls.append(args)
        return [record(PUBLIC if len(calls) == 1 else "127.0.0.1")]

    monkeypatch.setattr(net.socket, "getaddrinfo", resolver)
    with pytest.raises(net.PublicNetworkError, match="Non-public"):
        net.fetch_public("http://example.com/")
    assert len(calls) == 2
    assert len(sockets) == 1 and sockets[0].closed


@pytest.mark.parametrize("location", ["http://127.0.0.1/", "file:///secret", "http://user@evil.com/"])
def test_unsafe_redirects_refused(transport, monkeypatch, location):
    sockets, _, wires = transport
    wires.append(f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n\r\n".encode())
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda host, *args: [record(
        "127.0.0.1" if host == "127.0.0.1" else PUBLIC)])
    with pytest.raises(net.PublicNetworkError):
        net.fetch_public("http://example.com/")
    assert len(sockets) == 1


def test_redirect_limit_and_relative_success(transport):
    sockets, _, wires = transport
    redirect = b"HTTP/1.1 302 Found\r\nLocation: /next\r\n\r\n"
    wires.extend([redirect, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"])
    assert net.fetch_public("http://example.com/")["url"] == "http://example.com/next"
    wires.extend([redirect] * 6)
    with pytest.raises(net.PublicNetworkError, match="redirect limit"):
        net.fetch_public("http://example.com/")
    assert len(sockets) == 8


def test_head_returns_no_body_and_no_credentials(transport):
    sockets, _, wires = transport
    wires.append(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\nSet-Cookie: secret\r\n\r\n")
    result = net.fetch_public("http://example.com/", method="HEAD")
    assert result["body"] == b"" and result["headers"] == {}
    assert sockets[0].sent.startswith(b"HEAD ")


def test_response_sanitization_and_connection_tokens(transport):
    transport[2].append(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\n"
                        b"Set-Cookie: a=secret\r\nSet-Cookie: b=secret\r\n"
                        b"WWW-Authenticate: secret\r\nConnection: etag\r\nETag: secret\r\n"
                        b"Content-Encoding: identity\r\n\r\nok")
    result = net.fetch_public("http://example.com/")
    assert result["headers"] == {"content-type": "text/html"}


@pytest.mark.parametrize("header,body", [
    (b"Content-Encoding: gzip\r\n", b"compressed"),
    (b"Content-Encoding: br\r\n", b"compressed"),
    (b"Content-Length: 2\r\nContent-Length: 2\r\n", b"ok"),
    (b"Content-Length: -1\r\n", b"ok"),
    (b"Content-Length: 100\r\n", b"ok"),
    (b"Transfer-Encoding: gzip\r\n", b"ok"),
    (b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n", b"0\r\n\r\n"),
    (b"Location: /a\r\nLocation: /b\r\n", b""),
    (b"Content-Type: " + b"x" * 65536 + b"\r\n", b""),
    (b"X-Pad: " + b"x" * 35000 + b"\r\nX-Pad: " + b"x" * 35000 + b"\r\n", b""),
    (b"Content-Length: 4\r\n", b"bad"),
    (b"Content-Type: text/html\x00bad\r\n", b"ok"),
], ids=["gzip", "brotli", "duplicate-length", "negative-length", "oversize-length",
        "transfer-gzip", "ambiguous-framing", "duplicate-location", "long-line",
        "large-total", "truncated", "control-character"])
def test_malformed_or_unsafe_responses_fail_closed(transport, header, body):
    transport[2].append(b"HTTP/1.1 200 OK\r\n" + header + b"\r\n" + body)
    with pytest.raises(net.PublicNetworkError):
        net.fetch_public("http://example.com/", max_bytes=32)
    assert transport[0][0].closed


@pytest.mark.parametrize("framing", [b"", b"Transfer-Encoding: chunked\r\n"])
def test_streamed_size_limit(transport, framing):
    body = b"abcde" if not framing else b"5\r\nabcde\r\n0\r\n\r\n"
    transport[2].append(b"HTTP/1.1 200 OK\r\n" + framing + b"\r\n" + body)
    with pytest.raises(net.PublicNetworkError, match="too large"):
        net.fetch_public("http://example.com/", max_bytes=4)


def test_chunked_body_is_deframed(transport):
    transport[2].append(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                        b"2\r\nok\r\n0\r\nSet-Cookie: secret\r\n\r\n")
    result = net.fetch_public("http://example.com/")
    assert result["body"] == b"ok" and result["headers"] == {}


@pytest.mark.parametrize("body", [b"5\r\nbad", b"0\r\n", b"2\r\nok\r\n0\r\nX: y\r\n"])
def test_truncated_chunked_refused(transport, body):
    transport[2].append(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body)
    with pytest.raises(net.PublicNetworkError):
        net.fetch_public("http://example.com/")


@pytest.mark.parametrize("kwargs", [
    {"method": "POST"}, {"method": "CONNECT"}, {"method": "get"},
    {"max_bytes": -1}, {"max_bytes": net.MAX_BYTES + 1}, {"max_bytes": True},
    {"deadline": float("nan")}, {"deadline": time.monotonic() - 1},
    {"headers": {"Accept": "text/html\r\nCookie: secret"}},
    {"headers": {"Bad Header": "x"}},
])
def test_invalid_options_refused_before_network(transport, kwargs):
    with pytest.raises(net.PublicNetworkError):
        net.fetch_public("http://example.com/", **kwargs)
    assert not transport[0] and not transport[1]


def test_transport_errors_do_not_leak_secrets(transport, monkeypatch):
    def fail(*args):
        raise OSError("secret resolver detail")
    monkeypatch.setattr(net.socket, "getaddrinfo", fail)
    with pytest.raises(net.PublicNetworkError) as error:
        net.fetch_public("http://example.com/?token=secret")
    assert "secret" not in str(error.value)


def test_read_deadline_is_reapplied_during_stream(transport, monkeypatch):
    sockets, _, wires = transport
    wires.append(b"HTTP/1.1 200 OK\r\n\r\nok")
    clock = [100.0]
    monkeypatch.setattr(net.time, "monotonic", lambda: clock[0])
    original = MemorySocket.recv_into

    def slow_read(self, buffer):
        count = original(self, buffer)
        clock[0] += 31
        return count

    monkeypatch.setattr(MemorySocket, "recv_into", slow_read)
    with pytest.raises(net.PublicNetworkError, match="deadline"):
        net.fetch_public("http://example.com/")
    assert sockets[0].closed


def test_failed_pin_falls_back_only_to_validated_ipv4(transport, monkeypatch):
    sockets, _, _ = transport
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *args: [record(PUBLIC), record("8.8.8.8")])

    def connect(self, target):
        self.targets.append(target)
        if target[0] == PUBLIC:
            raise OSError("unreachable")

    monkeypatch.setattr(MemorySocket, "connect", connect)
    assert net.fetch_public("http://example.com/")["body"] == b"ok"
    assert [sock.targets for sock in sockets] == [[(PUBLIC, 80)], [("8.8.8.8", 80)]]
    assert all(sock.closed for sock in sockets)


def test_tls_verification_failure_is_not_downgraded(transport, monkeypatch):
    class BadCertificate:
        def wrap_socket(self, sock, server_hostname):
            raise ssl.SSLCertVerificationError("secret upstream certificate detail")

    monkeypatch.setattr(net.ssl, "create_default_context", lambda: BadCertificate())
    with pytest.raises(net.PublicNetworkError, match="Public transport failed") as caught:
        net.fetch_public("https://example.com/")
    assert caught.value.error_code == "network_tls_error"
    assert transport[0][0].closed
    assert not transport[0][0].sent


def test_dns_lookup_capacity_is_bounded(transport, monkeypatch):
    class NoSlots:
        def acquire(self, blocking):
            assert blocking is False
            return False

    monkeypatch.setattr(net, "_DNS_SLOTS", NoSlots())
    with pytest.raises(net.PublicNetworkError, match="capacity"):
        net.fetch_public("http://example.com/")
    assert not transport[0] and not transport[1]


def test_dns_timeout_is_safe_and_passes_finite_budget(transport, monkeypatch):
    lookup_finished = threading.Event()

    class TimeoutQueue:
        def __init__(self, maxsize):
            assert maxsize == 1

        def put(self, value):
            lookup_finished.set()

        def get(self, timeout):
            assert 0 < timeout <= 10
            assert lookup_finished.wait(2), "mock DNS worker did not complete"
            raise net.queue.Empty

    monkeypatch.setattr(net.queue, "Queue", TimeoutQueue)
    with pytest.raises(net.PublicNetworkError, match="DNS lookup timed out"):
        net.fetch_public("http://example.com/")
    assert not transport[0]


def test_informational_headers_share_total_budget(transport):
    transport[2].append((b"HTTP/1.1 100 Continue\r\nX-Pad: " + b"x" * 34000 + b"\r\n\r\n") * 2
                        + b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    with pytest.raises(net.PublicNetworkError, match="too large"):
        net.fetch_public("http://example.com/")
    assert transport[0][0].closed
