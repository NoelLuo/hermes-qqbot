# Build from the suite repository root; reviewed Linux/amd64 recipe.
# Official manifest digest, not a private host image ID.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:96b39581c89131729a7ecb8d532314af54c7f9bcc7a61fe15c7a9e77602acf59
COPY plugins/guest-workspace/browser-runtime/requirements.txt /opt/hermes/requirements.txt
RUN python3 -I -B -m pip install --disable-pip-version-check --no-cache-dir --ignore-installed --only-binary=:all: --require-hashes -r /opt/hermes/requirements.txt
COPY overlay/tools/guest_browser_worker.py /opt/hermes/guest_browser_worker.py
USER 65534:65534
ENV HOME=/tmp LANG=C.UTF-8
WORKDIR /tmp
ENTRYPOINT ["python3", "-I", "-B", "/opt/hermes/guest_browser_worker.py"]
