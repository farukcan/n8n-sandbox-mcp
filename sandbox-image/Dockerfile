# Image used for every agent sandbox (built automatically by the MCP server).
# Add or remove tools here; the server rebuilds the image when this file changes.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tini bash coreutils ca-certificates curl wget git jq unzip zip less file procps \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      requests httpx pandas numpy matplotlib beautifulsoup4 lxml \
      openpyxl pillow pyyaml python-dateutil tabulate

RUN useradd --create-home --uid 1000 --shell /bin/bash sandbox \
 && mkdir -p /workspace \
 && chown sandbox:sandbox /workspace

ENV HOME=/home/sandbox \
    PATH=/home/sandbox/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_WARN_SCRIPT_LOCATION=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

USER sandbox
WORKDIR /workspace
# tini reaps orphaned background processes so they do not pile up as zombies
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
