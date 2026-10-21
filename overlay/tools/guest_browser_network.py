"""Public-only, credential-free HTTP broker for a networkless guest browser.

No shared URL-safety configuration or proxy settings are consulted. DNS is checked
in full, then only validated numeric IPv4 addresses are dialled. ``deadline`` is
an absolute time.monotonic() deadline; every call also has a 30 second ceiling.
Compressed responses are deliberately refused rather than decompressed.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import math
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

MAX_BYTES = 32 * 1024 * 1024
MAX_HEADERS = 64 * 1024
MAX_URL = 8192
MAX_REDIRECTS = 5
_DNS_SLOTS = threading.BoundedSemaphore(8)
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_REQUEST_HEADERS = frozenset({"accept", "accept-language", "user-agent"})
_RESPONSE_HEADERS = frozenset({
    "content-type", "content-language", "cache-control", "expires", "etag",
    "last-modified", "access-control-allow-origin", "access-control-expose-headers",
    "vary", "accept-ranges", "content-range",
})
_TRANSITION = tuple(ipaddress.ip_network(n) for n in ("2001::/23", "2002::/16"))


class PublicNetworkError(Exception):
    """Safe refusal message; never includes upstream URLs, headers or exceptions."""


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PublicNetworkError("Public fetch deadline exceeded")
    return min(10.0, remaining)


def _parse_url(url):
    if (not isinstance(url, str) or not url or len(url) > MAX_URL
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)
            or "\\" in url or "#" in url):
        raise PublicNetworkError("Invalid public URL")
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username is not None:
            raise ValueError
        if "@" in p.netloc or "%" in p.netloc:
            raise ValueError
        port = 443 if p.scheme == "https" else 80
        host = p.hostname.encode("idna").decode("ascii").lower()
        # Only the canonical port for the selected protocol, with no alternative spelling.
        authority = f"[{host}]" if ":" in host else host
        if p.netloc.lower() not in {authority, f"{authority}:{port}"}:
            raise ValueError
        if ":" not in host and (len(host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.rstrip(".").split("."))):
            raise ValueError
        if host.rstrip(".") in {"metadata.google.internal", "metadata.goog"}:
            raise ValueError
        target = quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if p.query:
            target += "?" + quote(p.query, safe="/%?:@!$&'()*+,;=-._~")
        canonical = urlunsplit((p.scheme, authority, target.split("?", 1)[0],
                               target.split("?", 1)[1] if "?" in target else "", ""))
        return canonical, host, port, target, p.scheme == "https"
    except (ValueError, UnicodeError):
        raise PublicNetworkError("Invalid public URL") from None


def _public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise PublicNetworkError("Invalid DNS address") from None
    if (str(address) == "168.63.129.16"  # Azure platform virtual IP (not ordinary public web).
            or not address.is_global or address.is_multicast or address.is_reserved
            or address.is_loopback or address.is_link_local or address.is_unspecified):
        raise PublicNetworkError("Non-public network destination refused")
    if isinstance(address, ipaddress.IPv6Address):
        # Reject mapped, NAT64, 6to4, Teredo and other transition/special ranges.
        if (address not in ipaddress.ip_network("2000::/3")
                or any(address in net for net in _TRANSITION)):
            raise PublicNetworkError("IPv6 transition destination refused")
    return address


def _resolve(host, port, deadline):
    # getaddrinfo has no portable timeout. Bound both caller wait and outstanding
    # daemon lookups, so stalled OS DNS cannot exhaust an unbounded thread pool.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise PublicNetworkError("Public DNS lookup capacity exceeded")
    result = queue.Queue(maxsize=1)

    def lookup():
        try:
            result.put(socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM))
        except Exception:
            result.put(None)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=lookup, daemon=True, name="guest-public-dns").start()
    try:
        records = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise PublicNetworkError("Public DNS lookup timed out") from None
    if not records:
        raise PublicNetworkError("Public DNS lookup failed")
    pins = []
    for family, kind, proto, canon, sockaddr in records:
        address = _public_address(sockaddr[0])
        if family == socket.AF_INET and isinstance(address, ipaddress.IPv4Address):
            pins.append(str(address))
        elif family != socket.AF_INET6 or not isinstance(address, ipaddress.IPv6Address):
            raise PublicNetworkError("Invalid DNS address family")
    if not pins:
        raise PublicNetworkError("Public IPv4 destination required")
    return tuple(dict.fromkeys(pins))


def _dial(host, port, pins, tls, deadline):
    for pin in pins:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(_remaining(deadline))
            # No create_connection: it would perform a second DNS lookup.
            sock.connect((pin, port))
            if tls:
                sock.settimeout(_remaining(deadline))
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            return sock
        except (OSError, ssl.SSLError):
            sock.close()
        except PublicNetworkError:
            sock.close()
            raise
    raise PublicNetworkError("Public connection failed")


class _SocketReader(io.RawIOBase):
    def __init__(self, sock, deadline):
        super().__init__()
        self.sock = sock
        self.deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        return self.sock.recv_into(buffer)


class _FramingReader:
    def __init__(self, sock, deadline):
        self.reader = io.BufferedReader(_SocketReader(sock, deadline), buffer_size=8192)
        self.framing_bytes = 0

    def readline(self, limit=-1):
        line = self.reader.readline(min(MAX_HEADERS + 1, limit) if limit >= 0 else MAX_HEADERS + 1)
        self.framing_bytes += len(line)
        if self.framing_bytes > MAX_HEADERS:
            raise PublicNetworkError("Public response headers or framing too large")
        if not line.endswith(b"\r\n"):
            raise PublicNetworkError("Truncated or invalid public response framing")
        return line

    def read(self, size=-1):
        return self.reader.read(size)

    def read1(self, size=-1):
        return self.reader.read1(size)

    def close(self):
        self.reader.close()

    def flush(self):
        self.reader.flush()


class _ResponseSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def makefile(self, mode):
        return _FramingReader(self.sock, self.deadline)


def _request_headers(headers):
    clean = {"Accept-Encoding": "identity", "Connection": "close"}
    if headers is None:
        return clean
    if not isinstance(headers, dict) or len(headers) > 100:
        raise PublicNetworkError("Invalid public request headers")
    size = 0
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise PublicNetworkError("Invalid public request headers")
        size += len(key) + len(value)
        if size > MAX_HEADERS or not _TOKEN.fullmatch(key):
            raise PublicNetworkError("Invalid public request headers")
        if key.lower() in _REQUEST_HEADERS:
            if any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise PublicNetworkError("Invalid public request headers")
            clean[key.lower()] = value
    return clean


def _response_headers(response, max_bytes):
    pairs = response.getheaders()
    if sum(len(k) + len(v) + 4 for k, v in pairs) > MAX_HEADERS:
        raise PublicNetworkError("Public response headers too large")
    grouped = {}
    for key, value in pairs:
        if not _TOKEN.fullmatch(key) or any(ord(c) < 32 or ord(c) > 255 for c in value):
            raise PublicNetworkError("Invalid public response headers")
        grouped.setdefault(key.lower(), []).append(value.strip())
    lengths = grouped.get("content-length", [])
    if lengths and (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,12}", lengths[0])):
        raise PublicNetworkError("Invalid public response length")
    if lengths and int(lengths[0]) > max_bytes:
        raise PublicNetworkError("Public response body too large")
    transfer = grouped.get("transfer-encoding", [])
    if transfer and (lengths or transfer != ["chunked"]):
        raise PublicNetworkError("Unsafe public response framing")
    if grouped.get("content-encoding", ["identity"]) != ["identity"]:
        raise PublicNetworkError("Compressed public response refused")
    connection_tokens = {v.strip().lower() for line in grouped.get("connection", [])
                         for v in line.split(",")}
    clean = {key: ", ".join(values) for key, values in grouped.items()
             if key in _RESPONSE_HEADERS and key not in connection_tokens}
    locations = grouped.get("location", [])
    if len(locations) > 1:
        raise PublicNetworkError("Ambiguous public redirect")
    return clean, locations[0] if locations else None


def _fetch_hop(parsed, method, headers, max_bytes, deadline):
    _canonical, host, port, target, tls = parsed
    pins = _resolve(host, port, deadline)
    sock = _dial(host, port, pins, tls, deadline)
    response = None
    try:
        request = f"{method} {target} HTTP/1.1\r\nHost: {host}\r\n"
        request += "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        sock.settimeout(_remaining(deadline))
        sock.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(_ResponseSocket(sock, deadline), method=method)
        response.begin()
        if not 200 <= response.status <= 599:
            raise PublicNetworkError("Unsupported public response status")
        clean, location = _response_headers(response, max_bytes)
        if response.status in {301, 302, 303, 307, 308} and location:
            return response.status, clean, b"", location
        body = bytearray()
        if method != "HEAD":
            while True:
                _remaining(deadline)
                chunk = response.read1(min(64 * 1024, max_bytes + 1 - len(body)))
                if not chunk:
                    if response.chunked and response.fp is not None:
                        raise PublicNetworkError("Truncated public response body")
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise PublicNetworkError("Public response body too large")
            if response.length not in {None, 0}:
                raise PublicNetworkError("Truncated public response body")
        _remaining(deadline)
        return response.status, clean, bytes(body), None
    finally:
        if response is not None:
            response.close()
        sock.close()


def fetch_public(url: str, *, method="GET", headers=None, max_bytes=MAX_BYTES, deadline=None):
    """Fetch a public HTTP(S) resource, or raise a safe PublicNetworkError.

    Header forwarding is a small allowlist; authentication, cookies, referers,
    arbitrary application headers and all response Set-Cookie are discarded.
    Redirects are resolved and validated independently, at most five times.
    """
    try:
        if method not in {"GET", "HEAD"}:
            raise PublicNetworkError("Only public GET and HEAD are permitted")
        if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BYTES:
            raise PublicNetworkError("Invalid public response size limit")
        now = time.monotonic()
        if deadline is not None and (type(deadline) not in {int, float} or not math.isfinite(deadline)):
            raise PublicNetworkError("Invalid public fetch deadline")
        end = min(now + 30.0, deadline) if deadline is not None else now + 30.0
        _remaining(end)
        clean = _request_headers(headers)
        for hop in range(MAX_REDIRECTS + 1):
            parsed = _parse_url(url)
            status, response_headers, body, location = _fetch_hop(parsed, method, clean, max_bytes, end)
            if location is None:
                return {"status": status, "headers": response_headers, "body": body, "url": parsed[0]}
            if hop == MAX_REDIRECTS:
                raise PublicNetworkError("Public redirect limit exceeded")
            url = urljoin(parsed[0], location)
        raise PublicNetworkError("Public redirect limit exceeded")
    except PublicNetworkError:
        raise
    except Exception:
        raise PublicNetworkError("Public fetch failed") from None
