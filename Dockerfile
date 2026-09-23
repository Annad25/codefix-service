# codefix-service: the HTTP service. Repository code never runs in this container:
# every build/test run is a sibling container from the acceptance image
# (sandbox/Dockerfile), started through the mounted Docker socket with
# --network none and the grader's resource limits.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git bash ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Docker CLI only (the daemon is the host's, via /var/run/docker.sock).
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CODEFIX_SANDBOX=docker \
    CODEFIX_SANDBOX_IMAGE=acceptance:latest \
    CODEFIX_WORK_ROOT=/tmp/codefix-work \
    CODEFIX_TRACE_DIR=/app/runs

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=3)"
CMD ["python", "-m", "app", "--host", "0.0.0.0", "--port", "8000"]
