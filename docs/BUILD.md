# Portable runtime build source and acceptance

**Recipes supplied; fresh public-recipe Docker builds are not yet verified.** No image pull/build, daemon change, service change or gateway reload was performed while authoring these files. See [VERIFICATION](VERIFICATION.md). Build/provisioning is an operator opt-in network operation; runtime execution never pulls images automatically.

## Browser: reviewed source to a new local image

Prerequisites: Docker with Linux containers, BuildKit/Dockerfile-specific ignore support, Linux/amd64 execution, outbound access to MCR and files.pythonhosted.org, and a reviewed final public `overlay/tools/guest_browser_worker.py`. Do not use a private plugin/profile tree as build context.

From the **suite root**, in an operator terminal:

```bash
docker build --platform linux/amd64 \
  -f plugins/guest-workspace/browser-runtime/Dockerfile \
  -t hermes-guest-browser:review .
docker image inspect hermes-guest-browser:review --format '{{.Id}}'
```

The official base manifest is pinned in the Dockerfile. Exact Playwright/dependency artifact URLs and SHA256s are locked in `requirements.txt`; the package version must match the browsers baked into the official image. The build downloads only the locked wheels, not host-installed binaries, and installs using `python3 -I -B` / `--require-hashes`. The declared dependency closure targets CPython 3.12/Linux amd64. MCR/PyPI availability, Docker permissions/proxies and architecture remain operational prerequisites. Do not claim byte-for-byte image reproducibility: build metadata/tool versions can alter the resulting image ID even with pinned inputs.

Before configuration, record the actual full local `sha256:` ID from image inspection, not a tag or manifest digest assumed to be a local config ID. Replace `VERIFIED_BROWSER_IMAGE_ID` below with that value; these verification commands create disposable containers and must be run only when authorized:

```bash
docker run --rm --network none --read-only --user 65534:65534 \
  --cap-drop ALL --security-opt no-new-privileges \
  --entrypoint python3 VERIFIED_BROWSER_IMAGE_ID -I -B -c \
  'import hashlib,importlib.metadata as m; print(m.version("playwright")); print(hashlib.sha256(open("/opt/hermes/guest_browser_worker.py","rb").read()).hexdigest())'
python -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("overlay/tools/guest_browser_worker.py").read_bytes()).hexdigest())'
```

Require Playwright `1.63.0` and equal source/baked-worker SHA256, then run the actual host driver's Docker fixture tests and optional live broker tests. Hash equality alone does not prove runtime safety or delivery. The driver expects an immutable local image ID and worker path `/opt/hermes/guest_browser_worker.py`; set the reviewed plugin entry's `browser_image` to your ID only after acceptance. Keep `browser_enabled` false until then. Changing the worker requires rebuilding and rechecking the configured image.

## Python: minimal public replacement candidate

Inspection established that the historical Python runtime used `python3` from an **unrelated pre-existing application image**, not a dedicated redistributable sandbox build. The reported interpreter was Python 3.14.6. Its original build inputs cannot be reconstructed from that image ID alone. That private image/service and its mutable tag are **not** installation dependencies, and its ID is deliberately not published here.

A minimal **replacement candidate**, not the previously tested image, is provided in `docs/build/python.Dockerfile`. It pins official Python 3.14.6-slim-bookworm to this public Linux/amd64 manifest:

`sha256:ff83a535339812dd72e69c93b3c48ddf7c85a324d6330af5797c82a255dbeef4`

The registry tag's index digest was `sha256:4c92ffcde4dd6f1ff72a24518f49fd4990b27134987dfa31a733badde66df9f8`; the Dockerfile deliberately pins its amd64 child. Metadata was retrieved from Docker Hub's official `library/python` registry API, not derived from a host image. No extra packages are needed by the stdlib supervisor embedded in `tools/guest_workspace_docker.py`.

```bash
# From suite root; source-only context, no COPY/RUN dependencies.
docker build --platform linux/amd64 -f docs/build/python.Dockerfile \
  -t hermes-guest-python:review .
docker image inspect hermes-guest-python:review --format '{{.Id}}'
docker run --rm --network none --read-only --user 65534:65534 \
  --cap-drop ALL --security-opt no-new-privileges \
  --entrypoint python3 VERIFIED_PYTHON_IMAGE_ID -I -B -c \
  'import ctypes,resource,selectors,sys; print(sys.version); assert sys.version_info[:3] == (3,14,6)'
```

This Dockerfile creates no mounted data volumes and starts no application service. The host driver supplies `python3` supervisor execution, restrictive runtime flags and tmpfs. **The replacement must pass real driver tests before use**: stdlib/import compatibility, isolation, no network/subprocesses/threads, UTF-8 document bounds, timeout/oversized output cleanup, valid output recovery and no-host-fallback behavior. A version print is not that acceptance. Set `docker_image` only to the independently accepted actual local full ID.

## Build failure policy and licensing

If a base/blob/wheel is inaccessible or hashes fail, report the blocker. Do not replace it with a plausible hash, host-installed wheel, arbitrary image or a private archive. The online browser recipe still requires downloading the official base layers and four specified public wheels. Offline environments must stage those exact validated inputs through an operator-reviewed process; no fallback image-import utility is shipped or promised here.

No private local image ID proves reproducibility. No raw build receipt, real screenshot, wheel or Docker archive belongs in this source repository. Preserve upstream MIT notices and all relevant Python/Playwright/Chromium/container third-party notices. Installing these recipes does not enable a plugin or change/restart the gateway.
