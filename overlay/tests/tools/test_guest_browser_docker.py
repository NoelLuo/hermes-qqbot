"""Browser boundary tests, including real offline Docker Chromium (not mocks).

The real tests require the locally built hermes-guest-browser:1.63.0 image.
A missing daemon/image is a failure, never silently presented as a passing skip.
"""
import base64
import json
import subprocess
import threading
import time

import pytest

from tools import guest_browser_docker as browser
from tools.guest_browser_worker import validate_request
from tools.guest_workspace_docker import _docker_env


def _image():
    result = subprocess.run(["docker", "image", "inspect", "hermes-guest-browser:1.63.0", "--format", "{{.Id}}"],
                            env=_docker_env(), capture_output=True, text=True, timeout=10, check=True)
    return result.stdout.strip()


@pytest.fixture
def own_browser_containers(monkeypatch):
    """Observe only containers created by THIS test, not concurrent live browsers."""
    names = []
    command = browser._command

    def own_command(image, name):
        names.append(name)
        return command(image, name)

    monkeypatch.setattr(browser, '_command', own_command)
    return names


@pytest.mark.parametrize("actions", [
    [{"kind": "eval", "code": "1+1"}], [{"kind": "fill", "value": "secret"}],
    [{"kind": "click_link", "index": True}], [{"kind": "click_link", "index": 80}],
    [{"kind": "scroll", "pixels": 2001}], [{"kind": "read", "extra": "no"}],
    [{"kind": "read"}] * 9,
    [{"kind": "download", "url": "https://example.com/", "name": "../a.txt"}],
    [{"kind": "download", "url": "https://example.com/", "name": "a.exe"}],
    [{"kind": "download", "url": "https://example.com/", "name": "CON.txt"}],
])
def test_reject_untrusted_actions(actions):
    with pytest.raises(ValueError):
        validate_request("https://example.com/", actions)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "data:text/html,hello", "https://user:pass@example.com/", "javascript:alert(1)"])
def test_reject_non_public_schemes_credentials(url):
    with pytest.raises(ValueError):
        validate_request(url, [])


def test_decode_rejects_bad_artifacts():
    result = {"success": True, "title": "ok", "url": "https://example.com/", "text": "", "links": [],
              "artifacts": {"ok.txt": base64.b64encode(b"ok").decode(), "OK.txt": ""}}
    with pytest.raises(ValueError, match="collision"):
        browser._decode_result(result)
    result["artifacts"] = {"oops.exe": ""}
    with pytest.raises(ValueError):
        browser._decode_result(result)
    result["artifacts"] = {"ok.txt": "not base64"}
    with pytest.raises(ValueError):
        browser._decode_result(result)


def test_mutable_image_rejected_without_docker():
    result = browser._run("https://example.com/", [], image="latest", timeout=1, fetcher=None)
    assert not result["success"] and "sha256" in result["error"]


def test_real_offline_chromium_javascript_links_png_download_isolation(own_browser_containers):
    image = _image()
    requests = []
    inspected = []
    fixture = b'''<!doctype html><title>Initial</title><body><div id="proof">not executed</div>
      <a href="/next">Next</a><a href="javascript:alert(1)">Unsafe</a>
      <form method="post" action="/publish"><input name="password"></form>
      <script src="/fixture.js"></script></body>'''

    def fetch(url, **kwargs):
        requests.append((url, kwargs))
        if not inspected:
            assert len(own_browser_containers) == 1
            metadata = subprocess.run(["docker", "inspect", own_browser_containers[0]], capture_output=True, text=True,
                                      env=_docker_env(), timeout=5, check=True)
            inspected.append(json.loads(metadata.stdout)[0])
        if url.endswith("fixture.js"):
            body = b'''document.title=localStorage.getItem('visit')?'LEAKED':'Real Chromium JS';
              localStorage.setItem('visit','yes');document.cookie='ephemeral=only';
              document.querySelector('#proof').textContent='JS executed';
              document.querySelector('a').textContent='JS-generated link';
              document.body.dataset.webrtc=String(typeof RTCPeerConnection);
              fetch('/blocked-post',{method:'POST',body:'must not send'}).catch(()=>{});
              new WebSocket('wss://example.com/socket');'''
            content = "application/javascript"
        elif url.endswith("/next"):
            body = b"<title>Second page</title><body>GET anchor navigation works</body>"
            content = "text/html"
        elif url.endswith("/report.txt"):
            body, content = b"explicit GET download", "text/plain"
        else:
            body, content = fixture, "text/html"
        return {"success": True, "status": 200, "headers": {"content-type": content}, "body": body}

    first = browser._run("https://example.com/", [{"kind": "read"}, {"kind": "screenshot"},
                         {"kind": "download", "url": "https://example.com/report.txt", "name": "report.txt"}],
                         image=image, timeout=45, fetcher=fetch)
    assert first["success"], first
    assert first["title"] == "Real Chromium JS"
    assert "JS executed" in first["text"]
    assert first["links"] == [{"text": "JS-generated link", "url": "https://example.com/next"}]
    assert first["artifacts"]["screenshot-2.png"].startswith(b"\x89PNG\r\n\x1a\n")
    assert len(first["artifacts"]["screenshot-2.png"]) > 1000
    assert first["artifacts"]["report.txt"] == b"explicit GET download"
    assert all(kwargs["method"] == "GET" for _, kwargs in requests)
    assert all(not ({"cookie", "authorization", "proxy-authorization"} & set(kwargs["headers"])) for _, kwargs in requests)
    assert not any("blocked-post" in url or "socket" in url for url, _ in requests)
    metadata = inspected[0]
    host = metadata["HostConfig"]
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
    assert metadata["Config"]["User"] == "65534:65534"
    assert "ALL" in host["CapDrop"] and "no-new-privileges" in host["SecurityOpt"]
    assert not host["Binds"] and not host["PortBindings"]
    assert host["Memory"] == 768 * 1024 * 1024 and host["PidsLimit"] == 128
    assert host["NanoCpus"] == 1000000000
    assert set(host["Tmpfs"]) == {"/tmp", "/dev/shm"}
    assert not any("TOKEN" in entry or "PASSWORD" in entry for entry in metadata["Config"]["Env"])
    missing = subprocess.run(["docker", "inspect", metadata["Id"]], capture_output=True,
                             env=_docker_env(), timeout=5, check=False)
    assert missing.returncode != 0 and b"no such" in missing.stderr.lower()
    second = browser._run("https://example.com/", [{"kind": "click_link", "index": 0}],
                          image=image, timeout=45, fetcher=fetch)
    assert second["success"], second
    assert second["title"] == "Second page" and second["url"] == "https://example.com/next"
    third = browser._run("https://example.com/", [{"kind": "read"}], image=image, timeout=45, fetcher=fetch)
    assert third["success"] and third["title"] == "Real Chromium JS", third


def test_real_timeout_cleans_own_container(own_browser_containers):
    image = _image()
    called = threading.Event()
    release = threading.Event()

    def stalled_fetch(url, **kwargs):
        called.set()
        release.wait(timeout=10)
        return {"success": False}

    start = time.monotonic()
    try:
        result = browser._run("https://example.com/", [], image=image, timeout=3, fetcher=stalled_fetch)
        assert called.is_set()
        assert not result["success"] and "deadline" in result["error"].lower()
        assert time.monotonic() - start < 15
    finally:
        release.set()
    assert len(own_browser_containers) == 1
    missing = subprocess.run(["docker", "inspect", own_browser_containers[0]],
                             capture_output=True, env=_docker_env(), timeout=5, check=False)
    assert missing.returncode != 0 and b"no such" in missing.stderr.lower()


def test_real_default_public_broker_example_com():
    result = browser.run_browser_sandbox("https://example.com/", [{"kind": "screenshot"}], image=_image())
    assert result["success"], result
    assert result["title"] == "Example Domain"
    assert result["text"] and result["links"]
    assert result["artifacts"]["screenshot-1.png"].startswith(b"\x89PNG")


def test_fetch_rpc_strips_secrets_and_honors_remaining_budget():
    calls = []

    def fetch(url, **kwargs):
        calls.append(kwargs)
        return {"status": 200, "headers": {}, "body": b"ok"}

    event = {"event": "fetch", "id": 1, "url": "https://example.com/", "method": "GET",
             "headers": {"Cookie": "secret", "Authorization": "secret", "Proxy-Authorization": "secret",
                         "Accept": "text/html", "Referer": "https://private.invalid/"}}
    response = browser._fetch_response(event, fetch, time.monotonic() + 5, max_bytes=17)
    assert response["success"] and base64.b64decode(response["body"]) == b"ok"
    assert calls == [{"method": "GET", "headers": {"accept": "text/html"}, "max_bytes": 17,
                      "deadline": calls[0]["deadline"]}]
    event["method"] = "POST"
    with pytest.raises(ValueError):
        browser._fetch_response(event, fetch, time.monotonic() + 5)


def test_real_resource_request_budget_fails_closed():
    calls = []
    html = ("<body>budget" + "".join(f'<img src="/resource-{i}">' for i in range(140)) + "</body>").encode()

    def fetch(url, **kwargs):
        calls.append(url)
        return {"status": 200, "headers": {"content-type": "text/html"}, "body": html if url.endswith("/") else b""}

    result = browser._run("https://example.com/", [], image=_image(), timeout=45, fetcher=fetch)
    assert not result["success"] and "budget" in result["error"], result
    assert len(calls) == 128


def test_real_redirect_retains_final_url_and_relative_resource_base():
    seen = []

    def fetch(url, **kwargs):
        seen.append(url)
        final = 'https://example.com/final/page' if url.endswith('/start') else url
        if url.endswith('/relative.js'):
            body = b"document.title='Redirect JS executed'"
            content = 'application/javascript'
        else:
            body = b'<title>Redirect fixture</title><body><a href="child">Next</a><script src="relative.js"></script></body>'
            content = 'text/html'
        return {'status': 200, 'headers': {'content-type': content}, 'body': body, 'url': final}

    result = browser._run('https://example.com/start', [], image=_image(), timeout=45, fetcher=fetch)
    assert result['success'], result
    assert result['url'] == 'https://example.com/final/page'
    assert result['title'] == 'Redirect JS executed'
    assert result['links'] == [{'text': 'Next', 'url': 'https://example.com/final/child'}]
    assert 'https://example.com/final/relative.js' in seen
