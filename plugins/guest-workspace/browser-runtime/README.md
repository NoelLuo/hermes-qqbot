# Public browser runtime build source

This directory contains **source only**, not a browser installation, wheel bundle, Docker archive, raw build receipt or proof screenshot. It belongs to the experimental pinned-host suite; read [BUILD](../../../docs/BUILD.md) before using it.

## Provenance and dependency closure

The inspected deployment Dockerfile used the official Linux/amd64 image:

`mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:96b39581c89131729a7ecb8d532314af54c7f9bcc7a61fe15c7a9e77602acf59`

It installed Playwright 1.63.0, greenlet 3.3.2 (CPython 3.12 Linux wheel), pyee 13.0.1 and typing_extensions 4.15.0 from locally staged, hash-verified wheels. The public recipe instead downloads the **same pinned wheel artifacts** over HTTPS during an explicitly online build, with public SHA256 checks in `requirements.txt`. No host wheels are copied. The official base already contains matching browsers; the Playwright Python package still needs installing. No `playwright install` or system package update is performed.

The official manifest and the four hashes were checked against registry/PyPI metadata during documentation preparation. This is artifact metadata verification, **not** a successful fresh Docker build. The matching wheel/browser recipe is Linux/amd64 only; ARM and other Python ABIs are not supported by this lock. If any artifact is unavailable or the ABI mismatches, stop; do not substitute versions or invent digests.

The worker comes from the candidate's `overlay/tools/guest_browser_worker.py` at build time and is placed at `/opt/hermes/guest_browser_worker.py`, the path expected by the driver. It runs as UID/GID `65534:65534`, `HOME=/tmp`, workdir `/tmp`, with the existing `python3 -I -B` entry point. No VOLUME is declared. The build must be invoked from the suite root so that this explicit COPY source exists. The Dockerfile-specific ignore file minimizes context; use a Docker/BuildKit version supporting Dockerfile-specific ignore files, or manually stage only the allowlisted build inputs into a disposable context.

See [build commands and acceptance](../../../docs/BUILD.md). The final worker must be compared against the final public overlay, not against a historical worker hash. New builds produce their own local image IDs; no private deployment image ID is distributed or promised reproducible.

## Boundary

Fresh container/context per invocation; direct container networking disabled, public resources brokered on the host through restricted validated GET. Runtime hardening belongs to the host driver, not this Dockerfile alone. Chromium's inner sandbox is disabled and the container is the isolation boundary; website JavaScript still executes. No host browser sessions, credentials, mounts, accounts, generic clicks, forms, POST/upload or caller code are exposed. Full budgets, SSRF controls, privacy disclosure and residual risks are in [CAPABILITIES](../../../docs/CAPABILITIES.md) and [SECURITY](../../../docs/SECURITY.md).

The base image, Python packages and browser binaries retain their own licenses/notices. They are retrieved from public distributors, not vendored here. Do not publish private receipts or platform proofs as build inputs.
