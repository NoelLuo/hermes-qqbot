# Official python:3.13-slim-bookworm linux/amd64 platform manifest.
# Index: sha256:b4409e44a3e971621ee4bc2f3dc7a53859980685d261ab7a3a7bd4f0b84445c7
# Imported by fetch_official_base.py when daemon registry proxy is unavailable.
FROM python:3.13-slim-bookworm@sha256:b4409e44a3e971621ee4bc2f3dc7a53859980685d261ab7a3a7bd4f0b84445c7
ARG WORKER_SHA256
ARG HELPER_SHA256
LABEL org.hermes.office.runtime="1" \
      org.hermes.office.worker-sha256="${WORKER_SHA256}" \
      org.hermes.office.helper-sha256="${HELPER_SHA256}"
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash zip unzip poppler-utils fonts-noto-cjk \
    libreoffice-writer libreoffice-calc libreoffice-impress \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /opt/requirements.txt
RUN python3 -m venv /opt/office && /opt/office/bin/pip install --no-cache-dir -r /opt/requirements.txt
COPY guest_office_worker.py /opt/guest_office_worker.py
COPY office_cli.py /opt/office_cli.py
RUN printf '#!/bin/sh\nexec /opt/office/bin/python3 /opt/office_cli.py "$@"\n' > /opt/office/bin/office-cli \
    && chmod 0555 /opt/office/bin/office-cli /opt/guest_office_worker.py /opt/office_cli.py \
    && echo "${WORKER_SHA256}  /opt/guest_office_worker.py" | sha256sum -c - \
    && echo "${HELPER_SHA256}  /opt/office_cli.py" | sha256sum -c - \
    && /opt/office/bin/pip freeze > /opt/python-versions.txt \
    && (libreoffice --version; pdftotext -v 2>&1; bash --version) > /opt/app-versions.txt
# Compile from trusted sources at runtime; avoid shipping unusable wheel caches.
RUN /opt/office/bin/python3 -I -B -c "import pathlib,shutil; [shutil.rmtree(p) for p in list(pathlib.Path('/opt/office').rglob('__pycache__'))]"
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["/opt/office/bin/python3", "-I", "-B", "/opt/guest_office_worker.py"]
