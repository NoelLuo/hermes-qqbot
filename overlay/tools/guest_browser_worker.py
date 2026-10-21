"""Trusted offline Playwright worker; stdin/stdout are bounded NDJSON RPC only.

This module is baked into the pinned image. Page JavaScript is untrusted; none
of the action dictionaries is evaluated as Python or JavaScript.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
from urllib.parse import urlsplit

MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_LINE = 92 * 1024 * 1024
EXTENSIONS = {"txt", "csv", "json", "md", "pdf", "png", "jpg", "jpeg", "webp"}


def validate_url(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 for c in url):
        raise ValueError("Invalid HTTP(S) URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Only credential-free HTTP(S) URLs allowed")
    return url


def validate_name(name):
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,66}", name)
            or name.endswith((".", " ")) or name.rsplit(".", 1)[-1].lower() not in EXTENSIONS
            or re.match(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", name, re.IGNORECASE)):
        raise ValueError("Invalid artifact basename or extension")


def validate_request(url, actions):
    validate_url(url)
    if not isinstance(actions, list) or len(actions) > 8:
        raise ValueError("At most eight browser actions allowed")
    fields = {"read": {"kind"}, "click_link": {"kind", "index"},
              "scroll": {"kind", "pixels"}, "screenshot": {"kind"},
              "download": {"kind", "url", "name"}}
    for action in actions:
        if not isinstance(action, dict) or action.get("kind") not in fields or set(action) != fields[action["kind"]]:
            raise ValueError("Unsupported browser action or fields")
        kind = action["kind"]
        if kind == "click_link" and (type(action["index"]) is not int or not 0 <= action["index"] < 80):
            raise ValueError("Invalid link index")
        if kind == "scroll" and (type(action["pixels"]) is not int or not -2000 <= action["pixels"] <= 2000):
            raise ValueError("Invalid scroll distance")
        if kind == "download":
            validate_url(action["url"])
            validate_name(action["name"])


def emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class Broker:
    def __init__(self):
        self.sequence = 0
        self.lock = asyncio.Lock()

    async def fetch(self, url, method="GET", headers=None):
        validate_url(url)
        if method != "GET":
            raise ValueError("Only GET is permitted")
        async with self.lock:
            self.sequence += 1
            emit({"event": "fetch", "id": self.sequence, "url": url,
                  "method": method, "headers": headers or {}})
            line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_LINE + 1)
            if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
                raise ValueError("Broker protocol line limit/EOF")
            response = json.loads(line)
            if response.get("id") != self.sequence or response.get("success") is not True:
                raise ValueError("Public fetch denied or failed")
            data = base64.b64decode(response["body"], validate=True)
            if len(data) > MAX_FILE:
                raise ValueError("Fetch body limit exceeded")
            return response, data


async def snapshot(page):
    # Fixed trusted expressions only, never supplied by a caller.
    text = await page.locator("body").inner_text(timeout=5000)
    links = await page.locator("a[href]").evaluate_all("els => els.map(a => ({text:(a.innerText || '').slice(0,512),url:a.href})).filter(a => /^https?:/.test(a.url)).slice(0,80)")
    links = [link for link in links if _valid_url(link["url"])]
    return {"title": (await page.title())[:1024], "url": page.url,
            "text": text.encode("utf-8")[:32768].decode("utf-8", errors="ignore"), "links": links}


def _valid_url(url):
    try:
        validate_url(url)
        return True
    except ValueError:
        return False


async def browse(request):
    from playwright.async_api import Error as PlaywrightError, async_playwright
    validate_request(request["url"], request["actions"])
    broker = Broker()
    artifacts = {}
    total = 0
    navigation_redirect = None

    def artifact(name, data):
        nonlocal total
        validate_name(name)
        if name.casefold() in {n.casefold() for n in artifacts}:
            raise ValueError("Artifact name collision")
        if len(artifacts) >= 8 or len(data) > MAX_FILE or total + len(data) > MAX_TOTAL:
            raise ValueError("Artifact byte/count limit exceeded")
        total += len(data)
        artifacts[name] = base64.b64encode(data).decode("ascii")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, chromium_sandbox=False, args=[
            "--disable-dev-shm-usage", "--disable-background-networking",
            "--disable-extensions", "--disable-sync", "--no-first-run",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-features=WebRtcHideLocalIpsWithMdns,MediaRouter"])
        context = await browser.new_context(service_workers="block", accept_downloads=False,
                                            viewport={"width": 1280, "height": 720})
        await context.route_web_socket("**/*", lambda ws: ws.close())
        await context.add_init_script("Object.defineProperty(globalThis,'RTCPeerConnection',{value:undefined}); Object.defineProperty(globalThis,'webkitRTCPeerConnection',{value:undefined});")

        async def route_request(route):
            nonlocal navigation_redirect
            req = route.request
            if req.method != "GET" or not _valid_url(req.url):
                await route.abort("blockedbyclient")
                return
            try:
                response, data = await broker.fetch(req.url, req.method, await req.all_headers())
                if (response.get("redirect_url") and req.is_navigation_request()
                        and req.frame == page.main_frame):
                    # Chromium cannot follow a native redirect with network=none:
                    # abort this navigation and explicitly re-enter routing below.
                    navigation_redirect = validate_url(response["redirect_url"])
                    await route.abort("aborted")
                    return
                headers = {k: v for k, v in response["headers"].items()
                           if k.lower() not in {"set-cookie", "set-cookie2", "content-length", "content-encoding", "transfer-encoding", "connection", "location", "refresh"}}
                # Redirects are resolved and revalidated by the public broker.
                await route.fulfill(status=response["status"], headers=headers, body=data)
            except (ValueError, KeyError, TypeError):
                await route.abort("blockedbyclient")

        await context.route("**/*", route_request)
        page = await context.new_page()
        # No popup, permission, download, dialog or persistent state API exposed.
        context.on("page", lambda p: asyncio.create_task(p.close()) if p != page else None)
        page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))
        page.set_default_timeout(7000)

        async def navigate(target):
            nonlocal navigation_redirect
            for _ in range(6):
                navigation_redirect = None
                try:
                    await page.goto(target, wait_until="load", timeout=20000)
                except PlaywrightError:
                    if navigation_redirect is None:
                        raise
                if navigation_redirect is None:
                    return
                target = navigation_redirect
            raise ValueError("Browser navigation redirect limit")

        await navigate(request["url"])
        for index, action in enumerate(request["actions"]):
            kind = action["kind"]
            if kind == "click_link":
                current = await snapshot(page)
                if action["index"] >= len(current["links"]):
                    raise ValueError("Link index outside current DOM")
                await navigate(current["links"][action["index"]]["url"])
            elif kind == "scroll":
                await page.mouse.wheel(0, action["pixels"])
            elif kind == "screenshot":
                artifact(f"screenshot-{index + 1}.png", await page.screenshot(type="png", full_page=False))
            elif kind == "download":
                response, data = await broker.fetch(action["url"])
                if not 200 <= response["status"] < 300:
                    raise ValueError("Download HTTP failure")
                artifact(action["name"], data)
        result = await snapshot(page)
        await browser.close()
        return {"event": "result", "success": True, **result, "artifacts": artifacts}


def main():
    try:
        raw = sys.stdin.buffer.readline(65537)
        if len(raw) > 65536 or not raw.endswith(b"\n"):
            raise ValueError("Invalid initial request")
        result = asyncio.run(browse(json.loads(raw)))
    except Exception as exc:
        result = {"event": "result", "success": False, "title": "", "url": "", "text": "",
                  "links": [], "artifacts": {}, "error": str(exc)[:512]}
    emit(result)


if __name__ == "__main__":
    main()
