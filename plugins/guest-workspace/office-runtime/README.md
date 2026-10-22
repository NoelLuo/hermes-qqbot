# Isolated office runtime (operator build assets)

Experimental companion to the matched core overlay, not a host execution tool. The image supplies bash, zip/unzip, Poppler, CJK fonts, LibreOffice Writer/Calc/Impress and Python document libraries. Runtime has no network, host mounts or Docker socket; guest processes/threads are bounded and collected by a protected namespace supervisor before outputs are validated and transactionally persisted.

## Build and validate

Use Python 3.12+ and an operator-reviewed prepared Hermes source tree containing `tools/guest_office_worker.py`. The online builder takes explicit paths and refuses an existing evidence directory:

```bash
python plugins/guest-workspace/office-runtime/build_office.py --source /path/to/prepared-hermes --scratch /path/to/new-office-build
```

The Dockerfile pins an official Python 3.13 slim-bookworm registry index; select Linux/amd64. The complete twelve-wheel CPython 3.13/Linux dependency closure is version/SHA256 locked in `requirements.txt`. Debian application packages are resolved from signed Debian repositories at build time, not locked to a historical package snapshot. No bit-for-bit image reproducibility is claimed.

If daemon registry access fails, `fetch_official_base.py /path/to/new-base-directory` retrieves an official digest-verified OCI archive via host HTTPS. Inspect its reported platform/config digests, then explicitly `docker load` that archive. It includes no private runtime image or host configuration. The offline builder addresses the verified loaded official config ID, not a remotely pullable name:

```bash
python plugins/guest-workspace/office-runtime/build_office_offline.py /path/to/prepared-hermes /path/to/offline-build-cache
```

This optional builder requires `httpx`, a working `pip download` command and host HTTPS access to Debian/PyPI. Use a separate build environment; do not modify Hermes's managed application environment. It validates Debian signed indexes via APT, checks package hashes and wheel requirement hashes, and builds with `network=none`. `HERMES_OFFICE_BUILD_PROXY` is an optional **operator build-only** proxy; no local proxy address is hardcoded or passed into runtime. No listener, host mount, daemon proxy rewrite or service restart is used. Cache directories may be reused; raw logs and archives stay outside this source repository.

Before activation, inspect the actual local full image ID, baked worker/helper SHA256 and `/opt/app-versions.txt` / `/opt/python-versions.txt`. Compare the worker with `overlay/tools/guest_office_worker.py` and the helper with `office_cli.py`. Run the actual office store/Docker/live tests in the prepared host, including binary persistence/export, process cleanup, forbidden links/ZIP traversal, failure rollback and cross-requester isolation. Only then set `plugins.entries.guest-workspace.office_image` to that accepted full ID. A build success or a version print is not acceptance.

## Calling contract and limits

`guest_workspace(action="run_office", command=..., inputs=..., timeout=..., overwrite=...)` executes bash at `/work`. Omitted inputs snapshot ordinary requester files; `[]` supplies none. Timeout is 1..90 seconds, default 60; command is at most 32 KiB. Binary exchange is at most 256 files, 32 MiB each / 128 MiB combined. Root is read-only; runtime caps include 1 CPU, 1 GiB memory, 128 PIDs, 256 MiB `/work` tmpfs and 128 MiB `/tmp` tmpfs. Persistent storage quotas remain separate.

`office-cli zip-list`, `zip-extract` and `pdf-text` provide bounded ZIP/PDF helpers; run `office-cli --help` inside the runtime for the installed argument definitions. ZIP traversal, links, duplicate names and expanded-budget violations are rejected. PDF text results flag truncation and suspected scans. OCR and full Microsoft Office macro/layout compatibility are **not** promised. This capability does not relax the older UTF-8 small-document `run_python` tool.

Fresh builds of these newly published portable recipes have not been claimed; see the suite [verification ledger](../../../docs/VERIFICATION.md) for actual source-matched runtime tests and remaining build/QQ acceptance boundaries. Preserve all Python, LibreOffice, Poppler and other dependency licenses/notices.
