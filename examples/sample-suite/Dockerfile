# Example suite image: pytest tests packaged to run as a Kubernetes Job.
# Any image works if it has /bin/sh, base64 and tr, and writes JUnit XML to the path given by {junit}.
FROM python:3.12-slim

RUN pip install --no-cache-dir pytest==8.3.4 \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin runner

WORKDIR /suite
COPY --chown=10001:10001 . /suite
USER 10001
