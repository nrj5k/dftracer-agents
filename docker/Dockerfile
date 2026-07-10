# Production image for the dftracer agents stack:
#   :5000  dftracer-mcp-server   (streamable-HTTP MCP at /mcp)
#   :4318  dftracer-profile-collector (OTLP http/json)
#   :5001  mlflow tracking server + UI
#
#   docker build -f docker/Dockerfile -t dftracer/agents-stack .
#   docker compose up -d          # from the repo root

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1

# clang: needed by the clang_* annotation MCP tools.
# git: session_create clones apps. build-essential: sdist fallbacks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git clang build-essential ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user; /workspace is the project root the stack roots itself at
# (it looks for workspaces/ upward from cwd).
RUN useradd -m -u 1000 dftracer \
    && mkdir -p /workspace/workspaces \
    && chown -R dftracer:dftracer /workspace

COPY --chown=dftracer:dftracer . /opt/dftracer-agents
RUN pip install /opt/dftracer-agents mlflow

USER dftracer
WORKDIR /workspace

# Bind all three services on all interfaces so published ports reach them.
ENV DFTRACER_STACK_HOST=0.0.0.0 \
    MCP_PORT=5000 \
    COLLECTOR_PORT=4318 \
    MLFLOW_PORT=5001

EXPOSE 5000 4318 5001

# `start` daemonizes and health-checks the three services; `logs mcp` tails,
# which is what keeps the container in the foreground.
ENTRYPOINT ["bash", "-c", "dftracer_agents_stack start && exec dftracer_agents_stack logs mcp"]
