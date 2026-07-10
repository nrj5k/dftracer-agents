Docker: containerized stack with authenticated access
=======================================================

``docker-compose.yaml`` and ``docker/Dockerfile`` package the same three
services ``dftracer_agents_stack`` supervises on bare metal — the MCP server,
the OTLP profiling collector, and MLflow — into one image, fronted by a
`Caddy <https://caddyserver.com/>`_ reverse proxy (``docker/Caddyfile``) that
enforces authentication and is the only container port published to the host.

This is the path to use when you want a fixed, authenticated set of ports to
connect VS Code (locally, or over an SSH tunnel to a remote host) to, instead
of running ``dftracer_agents_stack start`` directly in a shell.

Why a proxy in front, instead of auth on each service
-------------------------------------------------------

The three services are heterogeneous: FastMCP's HTTP transport, a raw
``http.server`` OTLP receiver, and MLflow's own server. Rather than wiring
different auth mechanisms into each one, a single Caddy layer terminates auth
for all three uniformly:

- ``/mcp*`` and ``/v1/traces`` (etc.) — machine-to-machine, so a **bearer
  token** compared against the ``Authorization`` header before the request is
  proxied through.
- ``/mlflow*`` — a browser UI, so **HTTP basic-auth**, which every browser
  already knows how to prompt for.

The ``mcp``/``collector``/``mlflow`` containers sit on an internal-only Docker
network (``dftracer-net``) with no ``ports:`` of their own — they are not
reachable at all except through Caddy's checks.

Quick start
-----------

.. code-block:: bash

   cp .env.example .env
   # fill in the four auth variables — see "Generating the secrets" below
   docker compose up -d

Endpoints, once running (all through Caddy on ``127.0.0.1:8443``):

.. list-table::
   :header-rows: 1

   * - Endpoint
     - URL
     - Auth
   * - MCP
     - ``http://localhost:8443/mcp``
     - ``Authorization: Bearer $DFTRACER_MCP_TOKEN``
   * - OTLP collector
     - ``http://localhost:8443/v1/traces`` (``/v1/metrics``, ``/v1/logs``)
     - ``Authorization: Bearer $DFTRACER_COLLECTOR_TOKEN``
   * - MLflow UI
     - ``http://localhost:8443/mlflow``
     - HTTP basic-auth prompt

Generating the secrets
-----------------------

The bearer tokens are opaque random strings — any generator works:

.. code-block:: bash

   openssl rand -hex 32   # DFTRACER_MCP_TOKEN
   openssl rand -hex 32   # DFTRACER_COLLECTOR_TOKEN

MLflow's basic-auth is username + a bcrypt **hash** of the password, never the
plaintext. Pick a real password, then hash it with Caddy's own tool so the
plaintext never touches disk:

.. code-block:: bash

   docker run --rm caddy:2 caddy hash-password --plaintext '<your real password>'

The command prints a ``$2a$...`` bcrypt string — that goes into
``MLFLOW_PASSWORD_HASH``. Quote it in ``.env``:

.. code-block:: bash

   MLFLOW_BASIC_AUTH_USER=admin
   MLFLOW_PASSWORD_HASH='$2a$14$EO6jRQJaFFqB2pNdVWvJT.q57m8w4lDSmBKGRrwspuaKYLRCy2SrW'

Unquoted, Compose's ``.env`` interpolation treats the leading ``$2a$`` as a
variable reference and mangles the value.

When you actually log in to the MLflow UI, the browser's basic-auth prompt
wants the **plaintext** password you hashed — not the hash itself.

``.env`` is already listed in ``.gitignore``; never commit it. Generate
independent tokens/passwords per environment (laptop, remote host) rather than
reusing one set everywhere, so a leak on one machine does not compromise
another.

Connecting from VS Code
------------------------

``.vscode/mcp.json``:

.. code-block:: json

   {
     "servers": {
       "dftracer": {
         "type": "http",
         "url": "http://localhost:8443/mcp",
         "headers": { "Authorization": "Bearer ${env:DFTRACER_MCP_TOKEN}" }
       }
     }
   }

Connecting from Claude Code
-----------------------------

.. code-block:: bash

   claude mcp add --transport http dftracer http://localhost:8443/mcp \
     --header "Authorization: Bearer $DFTRACER_MCP_TOKEN"

For pipeline profiling telemetry to reach the containerized collector, export
before launching the client:

.. code-block:: bash

   export CLAUDE_CODE_ENABLE_TELEMETRY=1
   export OTEL_LOGS_EXPORTER=otlp
   export OTEL_METRICS_EXPORTER=otlp
   export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
   export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8443
   export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $DFTRACER_COLLECTOR_TOKEN"

See :doc:`run` for the VS Code Remote-SSH caveat about *where* these
``OTEL_*`` variables must be exported (the server's real process environment,
not ``~/.bashrc``) — the same rule applies whether the collector is bare-metal
or containerized.

Reaching a remote instance
-----------------------------

The stack — bare-metal or containerized — only ever binds its ports to
``127.0.0.1``. It is never published on a public interface, authenticated or
not. To reach an instance running on a remote host, tunnel the single Caddy
port over SSH and connect to ``localhost`` exactly as documented above:

.. code-block:: bash

   ssh -N -L 8443:localhost:8443 <remote-host>

This also works to reach a Docker stack running on a *different* local
machine, or to run local and remote instances side by side (register the
local one on ``8443`` and tunnel the remote one to a different local port,
e.g. ``-L 18443:localhost:8443``, as a second MCP server entry).

Files
-----

- ``docker/Dockerfile`` — builds the image (``pip install .`` + ``mlflow``,
  non-root user, entrypoint runs ``dftracer_agents_stack start`` then tails
  the MCP log).
- ``docker-compose.yaml`` — the ``dftracer`` service (internal network only)
  and the ``caddy`` service (the only published port).
- ``docker/Caddyfile`` — the reverse-proxy routing and auth rules.
- ``.env.example`` — template for the four auth variables (appended alongside
  the repo's existing sandbox variables).
