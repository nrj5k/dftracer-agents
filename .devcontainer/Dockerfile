FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Base OS packages, Python toolchain, and common dev utilities.
RUN apt-get update
RUN apt-get install -y --no-install-recommends bash ca-certificates curl git less procps sudo iproute2 jq vim
RUN apt-get install -y --no-install-recommends python3 python3-pip python3-venv python3-dev build-essential
RUN apt-get install -y --no-install-recommends clang vim jq
RUN rm -rf /var/lib/apt/lists/*

# Node.js 20.x (goose tui requires node >=20)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Provide python/pip aliases expected by tooling.
RUN ln -sf /usr/bin/python3 /usr/local/bin/python && ln -sf /usr/bin/pip3 /usr/local/bin/pip

# Create a non-root user expected by devcontainer.json.
RUN if ! getent group vscode >/dev/null; then groupadd vscode; fi && if ! id -u vscode >/dev/null 2>&1; then useradd -m -s /bin/bash -g vscode vscode; fi && echo "vscode ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/vscode && chmod 0440 /etc/sudoers.d/vscode

# Install Goose CLI noninteractively and place it on a stable global path.
RUN mkdir -p /usr/local/bin && curl -fsSL https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh | CONFIGURE=false GOOSE_BIN_DIR=/usr/local/bin bash
