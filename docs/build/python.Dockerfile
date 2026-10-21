# Public minimal replacement candidate, not a recreation of a private deployment.
# linux/amd64 manifest for official Python 3.14.6-slim-bookworm.
FROM python:3.14.6-slim-bookworm@sha256:ff83a535339812dd72e69c93b3c48ddf7c85a324d6330af5797c82a255dbeef4
USER 65534:65534
ENV HOME=/tmp LANG=C.UTF-8
WORKDIR /tmp
ENTRYPOINT ["python3"]
CMD []
