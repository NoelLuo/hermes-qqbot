"""Disposable offline Chromium containers with a host-side public GET broker.

Not a general browser automation or execution API. Callers provide only the
trusted configuration image ID; never pass an image/flags obtained from a model.
No account state, forms, POST, uploads, real clicks, or arbitrary JS are exposed.
"""
from __future__ import annotations

import base64
import json
import logging
import queue
import re
import secrets
import subprocess
import threading
import time

from tools.guest_browser_worker import ERROR_CODES, MAX_FILE, MAX_TOTAL, validate_name, validate_request, validate_url
from tools.guest_workspace_docker import _docker_env, _exchange

_MAX_LINE = 92 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(2)
_SAFE_REQUEST_HEADERS = {"accept", "accept-language", "user-agent", "range"}


def _command(image, name):
    return ["docker", "run", "--rm", "--pull=never", "--name", name,
            "--network", "none", "--read-only", "--user", "65534:65534",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--cpus", "1", "--memory", "768m",
            "--memory-swap", "768m", "--ulimit", "core=0:0",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777",
            "--tmpfs", "/dev/shm:rw,noexec,nosuid,nodev,size=128m,mode=1777",
            "--env", "HOME=/tmp", "--env", "LANG=C.UTF-8",
            "--workdir", "/tmp", "--log-driver", "none", "--entrypoint", "python3",
            "-i", image, "-I", "-B", "/opt/hermes/guest_browser_worker.py"]


_ERROR_MESSAGES = {
    "network_connect_timeout": "Public network connection timed out.",
    "network_connect_failed": "Public network connection failed.",
    "network_tls_error": "Public TLS handshake or certificate validation failed.",
    "network_read_timeout": "Public response timed out.",
    "network_error": "Public network fetch refused or failed.",
    "http_error": "The public server returned an HTTP error.",
    "navigation_timeout": "Browser navigation deadline exceeded.",
    "unsupported_document": "PDF is not an HTML page. Use download(url,name) and process it with the isolated office worker.",
    "download_failed": "Download content did not match the requested document type.",
    "worker_failure": "Isolated browser worker failed; no host fallback.",
    "cleanup_failed": "Browser cleanup could not be verified; no outputs saved.",
}
_LOG = logging.getLogger(__name__)


def _failure(message=None, error_code="worker_failure"):
    if error_code not in ERROR_CODES:
        error_code = "worker_failure"
    return {"success": False, "title": "", "url": "", "text": "", "links": [],
            "artifacts": {}, "error_code": error_code, "error": _ERROR_MESSAGES[error_code]}


def _decode_result(value):
    if not isinstance(value, dict) or type(value.get("success")) is not bool:
        raise ValueError("Invalid browser response")
    if not value["success"]:
        return _failure(error_code=value.get("error_code", "worker_failure"))
    for field, limit in (("title", 4096), ("url", 8192), ("text", 32768)):
        if not isinstance(value.get(field), str) or len(value[field].encode("utf-8")) > limit:
            raise ValueError("Browser output limit exceeded")
    validate_url(value["url"])
    links = value.get("links")
    if not isinstance(links, list) or len(links) > 80:
        raise ValueError("Invalid browser links")
    for link in links:
        if not isinstance(link, dict) or set(link) != {"text", "url"}:
            raise ValueError("Invalid browser link")
        validate_url(link["url"])
        if not isinstance(link["text"], str) or len(link["text"]) > 512:
            raise ValueError("Invalid link text")
    encoded = value.get("artifacts")
    if not isinstance(encoded, dict) or len(encoded) > 8:
        raise ValueError("Invalid artifact count")
    artifacts = {}
    total = 0
    for name, data in encoded.items():
        validate_name(name)
        if name.casefold() in {n.casefold() for n in artifacts}:
            raise ValueError("Artifact name collision")
        if not isinstance(data, str) or len(data) > (MAX_FILE + 2) // 3 * 4:
            raise ValueError("Encoded artifact limit exceeded")
        decoded = base64.b64decode(data, validate=True)
        total += len(decoded)
        if len(decoded) > MAX_FILE or total > MAX_TOTAL:
            raise ValueError("Artifact byte limit exceeded")
        artifacts[name] = decoded
    return {"success": True, "title": value["title"], "url": value["url"],
            "text": value["text"], "links": links, "artifacts": artifacts,
            **_result_metadata(value)}


def _result_metadata(value):
    result_type = value.get("result_type", "page")
    if result_type not in {"page", "download"}:
        raise ValueError("Invalid browser result type")
    downloads = value.get("downloads", [])
    if not isinstance(downloads, list) or len(downloads) > 8:
        raise ValueError("Invalid downloads metadata")
    clean = []
    for item in downloads:
        if not isinstance(item, dict) or set(item) != {"name", "url", "content_type", "bytes", "http_status"}:
            raise ValueError("Invalid download metadata")
        validate_name(item["name"])
        validate_url(item["url"])
        if (item["name"] not in value["artifacts"] or type(item["bytes"]) is not int
                or not 0 <= item["bytes"] <= MAX_FILE or type(item["http_status"]) is not int
                or not 200 <= item["http_status"] < 300 or not isinstance(item["content_type"], str)
                or not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", item["content_type"])):
            raise ValueError("Invalid download metadata")
        if len(base64.b64decode(value["artifacts"][item["name"]], validate=True)) != item["bytes"]:
            raise ValueError("Download size mismatch")
        clean.append(dict(item))
    metadata = {"result_type": result_type, "downloads": clean}
    if "rendering" in value:
        if value["rendering"] != "static_html" or not isinstance(value.get("warning"), str) or len(value["warning"]) > 256:
            raise ValueError("Invalid rendering metadata")
        metadata.update(rendering="static_html", warning=value["warning"])
    if "http_status" in value:
        if type(value["http_status"]) is not int or not 200 <= value["http_status"] < 400:
            raise ValueError("Invalid page HTTP status")
        metadata["http_status"] = value["http_status"]
    if "content_type" in value:
        if not isinstance(value["content_type"], str) or len(value["content_type"]) > 128:
            raise ValueError("Invalid content type")
        metadata["content_type"] = value["content_type"]
    return metadata


def _fetch_response(event, fetcher, deadline, max_bytes=MAX_FILE):
    if (set(event) != {"event", "id", "url", "method", "headers"}
            or type(event["id"]) is not int or event["id"] < 1 or event["method"] != "GET"):
        raise ValueError("Invalid broker request")
    validate_url(event["url"])
    raw_headers = event["headers"]
    if not isinstance(raw_headers, dict) or len(raw_headers) > 128:
        raise ValueError("Invalid request headers")
    headers = {}
    for key, value in raw_headers.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(key) + len(value) > 16384 or "\r" in value or "\n" in value:
            raise ValueError("Invalid request header")
        if key.lower() in _SAFE_REQUEST_HEADERS:
            headers[key.lower()] = value
    try:
        response = fetcher(event["url"], method="GET", headers=headers,
                           max_bytes=max_bytes, deadline=deadline)
    except Exception as exc:
        code = getattr(exc, "error_code", "network_error")
        return {"id": event["id"], "success": False,
                "error_code": code if code in ERROR_CODES else "network_error"}
    if not isinstance(response, dict) or response.get("success") is False:
        return {"id": event["id"], "success": False}
    status, body, response_headers = response.get("status"), response.get("body"), response.get("headers")
    if type(status) is not int or not 200 <= status <= 599 or not isinstance(body, bytes) or len(body) > max_bytes:
        raise ValueError("Invalid broker response body/status")
    if not isinstance(response_headers, dict) or len(response_headers) > 128:
        raise ValueError("Invalid broker response headers")
    if any(not isinstance(k, str) or not isinstance(v, str) or len(k) + len(v) > 16384 or any(c in k + v for c in "\r\n") for k, v in response_headers.items()):
        raise ValueError("Invalid broker response header")
    reply = {"id": event["id"], "success": True, "status": status, "headers": response_headers,
             "body": base64.b64encode(body).decode("ascii")}
    final_url = response.get("url", event["url"])
    validate_url(final_url)
    reply["final_url"] = final_url
    if final_url != event["url"]:
        # Let Chromium observe the broker-validated redirect too, so origin,
        # relative resources and the reported final URL stay correct.
        reply["redirect_url"] = final_url
    return reply


def _browser_exchange(command, payload, deadline, env, fetcher):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, shell=False)
    events = queue.Queue(maxsize=2)
    failed = threading.Event()
    diagnostics = bytearray()

    def read_stdout():
        try:
            total = 0
            while True:
                line = process.stdout.readline(_MAX_LINE + 1)
                total += len(line)
                if not line:
                    events.put(None, timeout=1)
                    return
                if len(line) > _MAX_LINE or total > _MAX_LINE + 2 * 1024 * 1024 or not line.endswith(b"\n"):
                    raise ValueError("Browser protocol output limit")
                events.put(json.loads(line), timeout=1)
        except (ValueError, OSError, queue.Full):
            failed.set()

    def read_stderr():
        try:
            while True:
                chunk = process.stderr.read(8192)
                if not chunk:
                    return
                if len(diagnostics) + len(chunk) > 65536:
                    failed.set()
                    return
                diagnostics.extend(chunk)
        except OSError:
            failed.set()

    def protocol_write(data):
        process.stdin.write(json.dumps(data, ensure_ascii=True).encode() + b"\n")
        process.stdin.flush()

    threads = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        protocol_write(payload)
        last_id = 0
        count = 0
        fetched_bytes = 0
        while time.monotonic() < deadline:
            if failed.is_set():
                raise RuntimeError("Browser protocol pipe/size failure")
            try:
                event = events.get(timeout=min(.1, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if not isinstance(event, dict):
                raise RuntimeError("Browser exited without result: " + diagnostics.decode(errors="replace")[:512])
            if event.get("event") == "result":
                result = _decode_result(event)
                process.stdin.close()
                process.wait(timeout=max(.001, deadline - time.monotonic()))
                for thread in threads:
                    thread.join(timeout=1)
                trailing = []
                while not events.empty():
                    trailing.append(events.get_nowait())
                if process.returncode or failed.is_set() or any(t.is_alive() for t in threads) or trailing != [None]:
                    raise RuntimeError("Browser exited with invalid protocol/status")
                return result
            count += 1
            if event.get("event") != "fetch" or count > 128 or event.get("id") != last_id + 1:
                raise ValueError("Invalid browser event/request budget")
            last_id += 1
            # The entire exchange runs in a supervisor thread. The outer timer
            # remains effective even if a trusted fetch implementation stalls.
            if fetched_bytes >= MAX_TOTAL:
                raise ValueError("Browser aggregate fetch byte budget exhausted")
            response = _fetch_response(event, fetcher, deadline, min(MAX_FILE, MAX_TOTAL - fetched_bytes))
            encoded_body = response.get("body", "")
            fetched_bytes += len(encoded_body) // 4 * 3 - (len(encoded_body) - len(encoded_body.rstrip("=")))
            if fetched_bytes > MAX_TOTAL:
                raise ValueError("Browser aggregate fetch byte budget exceeded")
            protocol_write(response)
        raise TimeoutError("Browser deadline exceeded")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if not pipe.closed:
                pipe.close()


def _remove(name, env):
    subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=5, env=env, check=False)
    status, _, error = _exchange(["docker", "container", "inspect", name], b"", 5, env)
    if status != 1 or b"No such" not in error:
        raise RuntimeError("Browser container cleanup not verified")


def _run(url, actions, *, image, timeout, fetcher):
    result = _failure("Browser did not run")
    name = None
    acquired = False
    env = _docker_env()
    correlation_id = secrets.token_hex(8)
    started = time.monotonic()
    try:
        validate_request(url, actions)
        if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Trusted immutable full sha256 image ID required")
        if type(timeout) is not int or not 1 <= timeout <= 60:
            raise ValueError("Timeout must be an integer from 1 to 60")
        acquired = _SLOTS.acquire(blocking=False)
        if not acquired:
            raise RuntimeError("Browser concurrency limit reached")
        status, raw, _ = _exchange(["docker", "image", "inspect", "--format", "{{json .}}", image], b"", 5, env)
        if status:
            raise RuntimeError("Trusted browser image unavailable")
        info = json.loads(raw)
        if info.get("Id") != image or info.get("Os") != "linux" or info.get("Config", {}).get("Volumes"):
            raise ValueError("Browser image must be Linux without volumes")
        name = "hermes-guest-browser-" + secrets.token_hex(16)
        deadline = time.monotonic() + timeout
        output = queue.Queue(maxsize=1)

        def supervise():
            try:
                output.put(_browser_exchange(_command(image, name), {"url": url, "actions": actions}, deadline, env, fetcher))
            except Exception as exc:
                output.put(_failure(error_code="navigation_timeout" if isinstance(exc, TimeoutError) else "worker_failure"))
            finally:
                # A timed-out caller must not free capacity while a broker
                # operation is still unwinding in this supervisor.
                _SLOTS.release()

        thread = threading.Thread(target=supervise, daemon=True)
        thread.start()
        acquired = False  # The running supervisor now owns the slot.
        try:
            result = output.get(timeout=timeout)
        except queue.Empty:
            result = _failure(error_code="navigation_timeout")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result = _failure(exc)
    finally:
        if name is not None:
            try:
                _remove(name, env)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                original = result.get("error_code") if not result.get("success") else None
                result = _failure(error_code="cleanup_failed")
                if original:
                    result["cause_error_code"] = original
        if acquired:
            _SLOTS.release()
    result["correlation_id"] = correlation_id
    # Bounded structured fields only: never exception text, URLs, IDs, cookies or bodies.
    _LOG.info("guest_browser correlation=%s outcome=%s cause=%s elapsed_ms=%d",
              correlation_id, "success" if result.get("success") else result.get("error_code"),
              result.get("cause_error_code", "none"), int((time.monotonic() - started) * 1000))
    return result


def run_browser_sandbox(url: str, actions: list[dict], *, image: str, timeout: int = 45) -> dict:
    """Fresh offline browser, public GET broker, bounded text/links/byte artifacts.

    Actions: read; click_link(index, current DOM anchor only, navigates by GET);
    scroll(pixels); screenshot; download(url,name). No login/fill/POST or generic
    clicks/JS. Up to 8 actions/artifacts, 32 MiB/file and 64 MiB total. Chromium's
    inner sandbox is disabled for compatibility with Docker's capability policy;
    the nonroot, network-none, read-only resource-limited container is the boundary.
    Website JS executes, but all its HTTP(S) requests are brokered without account
    cookies/auth. WebSockets/WebRTC/service workers are unavailable. Redirects are
    revalidated by the broker and reflected into Chromium's navigation. Downloads
    are explicit GET only; filename extensions do not establish content safety.
    """
    from tools.guest_browser_network import fetch_public
    return _run(url, actions, image=image, timeout=timeout, fetcher=fetch_public)
