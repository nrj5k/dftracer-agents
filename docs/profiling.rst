Pipeline profiling
==================

Every agent step in a dftracer pipeline is measured: how long it ran, how many
times it was tried, how many AI tokens it consumed, and what that cost in USD.
The results are written into the session and mirrored into MLflow.

Running the stack
-----------------

``dftracer_agents_stack`` starts the three long-lived processes together and
prints the environment Claude Code needs to feed the profiler:

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Service
     - Default port
     - Role
   * - ``mlflow``
     - 5001
     - Tracking server and UI, backed by SQLite
   * - ``collector``
     - 4318
     - Receives Claude Code OTLP telemetry, attributes it to pipeline steps
   * - ``mcp``
     - 5000
     - The dftracer MCP tool surface, auto-reloading on source edits

.. code-block:: bash

   dftracer_agents_stack start           # start or refresh everything
   eval "$(dftracer_agents_stack env)"   # export the OTEL_* variables
   claude                                # now profiled

   dftracer_agents_stack status          # up? stale? what has the run cost?
   dftracer_agents_stack clean           # stale pid files, orphaned daemons
   dftracer_agents_stack client          # re-point harnesses at the server
   dftracer_agents_stack logs collector  # mlflow | collector | mcp | graph
   dftracer_agents_stack stop            # collector flushes its profile first

``start`` is idempotent. A service that is healthy and configured identically is
left running; one that died, stopped answering its port, or whose configuration
changed is restarted. A port held by a process the stack did not start is never
touched. This makes ``start`` safe to re-run as a refresh.

The stack owns the server
-------------------------

Every harness will, by default, spawn its **own private stdio MCP server** from a
``command`` entry in its config — ignoring the one the stack manages. Two servers
then run: the managed one nobody talks to, and an unmanaged one that reloads
nothing. ``start`` prevents that by rewriting each harness's entry to a ``url``:

.. list-table::
   :header-rows: 1
   :widths: 22 34 44

   * - Harness
     - Config file
     - Entry written
   * - Claude Code
     - ``.mcp.json``
     - ``mcpServers.dftracer.url``
   * - GitHub Copilot
     - ``.vscode/mcp.json``
     - ``servers.dftracer.url``
   * - OpenCode
     - ``.opencode/opencode.jsonc``
     - ``mcp.dftracer.url``

``opencode.jsonc`` holds provider and model settings alongside comments that a
JSON round-trip would delete, so when comments are present the snippet to paste
is printed rather than the file rewritten.

Restart the harness after the first ``start``: a client reads its MCP config only
at launch. Afterwards ``status`` should report no untracked ``mcp`` process.

.. note::

   Only Claude Code emits the ``claude_code.*`` telemetry this profile is built
   on. OpenCode and Copilot can use the managed MCP server, but their steps will
   not appear in the cost accounting.

Stale and orphaned processes
----------------------------

Pid files record what the launcher *believes* is running. ``status`` reconciles
them against ``/proc`` and names what they miss:

**stale pid file**
   The process died; the file outlived it. ``clean`` removes it.

**orphan**
   A supervisor (``dftracer-mcp-server --reload``) was killed but the child that
   binds the port survived, or a daemon of one of our services is sitting on a
   port this stack manages. Either way it is ours, and ``clean`` reaps it — with
   ``SIGTERM`` before ``SIGKILL``, so the collector still flushes its profile and
   closes its MLflow run on the way out.

**untracked**
   A daemon this stack did not start: a harness's own stdio server, or a
   colleague's process on a shared node. Reported, and never killed without an
   explicit ``clean --untracked``.

A process is only ever a cleanup candidate when its command line identifies it as
one of our daemons **and** it belongs to the current user. An MLflow server is
ours only if it references this workspaces root, so a site-wide tracking server
on the same node is never mistaken for an orphan.

dftracer-agents must be installed into a venv or conda environment. The launcher
resolves its daemons from that environment's ``bin/`` directory; it will not
silently fall back to the system Python.

.. note::

   The MCP server asks once, interactively, which harness and models to use. A
   backgrounded daemon cannot answer that, so run ``dftracer-configure-harness``
   before the first ``dftracer_agents_stack start``.

Where the numbers come from
---------------------------

The MCP server cannot observe the agent's LLM usage — it only sees tool calls.
The data therefore comes from Claude Code's own OpenTelemetry export, which the
stack's environment enables:

.. code-block:: bash

   CLAUDE_CODE_ENABLE_TELEMETRY=1
   OTEL_LOGS_EXPORTER=otlp
   OTEL_METRICS_EXPORTER=otlp
   OTEL_EXPORTER_OTLP_PROTOCOL=http/json
   OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318

``http/json`` is required, not merely preferred: the collector is a stdlib HTTP
server and cannot decode the protobuf bodies that ``grpc`` or ``http/protobuf``
would send.

Two events carry everything the profile needs:

``claude_code.api_request``
   ``cost_usd``, ``duration_ms``, ``input_tokens``, ``output_tokens``,
   ``cache_read_tokens``, ``cache_creation_tokens``, ``model``, and the
   ``agent.name`` that issued the request.

``claude_code.tool_result``
   ``tool_name``, ``duration_ms``, ``success`` — per-tool timings and failures,
   including every MCP tool.

Attribution is by **timestamp**, not by arrival. Claude Code buffers log events
and flushes them every ``OTEL_LOGS_EXPORT_INTERVAL`` milliseconds (5 s by
default), so a request made during step 3 routinely reaches the collector after
step 4 has begun. Each event is folded into whichever step attempt's time
interval contains its timestamp. Telemetry belonging to no explicit step — the
planning and routing turns — is attributed to a synthetic ``main`` step, so the
totals always reconcile to the whole session.

Marking steps and retries
-------------------------

Agents delimit steps with the ``profile_*`` MCP tools.

.. list-table::
   :header-rows: 1
   :widths: 28 72

   * - Tool
     - Purpose
   * - ``profile_bind``
     - Attach the profile to a session; call once after ``session_create``
   * - ``profile_step_begin``
     - Open a step. Calling it again with the **same step name records a retry**
   * - ``profile_step_end``
     - Close the current attempt with an outcome (``ok``, ``lint_failed``, …)
   * - ``profile_status``
     - Running totals: cost, tokens, per-step timing, tries, retries
   * - ``profile_report``
     - Flush and return the rendered performance report

A retry is a second *attempt* at the same step, never a duplicate step. That is
what lets the report distinguish "the annotator took 400 s" from "the annotator
took 400 s across three tries, two of which failed lint".

Two clocks are recorded per step, and they differ:

``exec_s``
   The sum of the attempts' durations — the time actually spent working. A retry
   inflates this.

``wall_s``
   First attempt's start to last attempt's end, including the gaps between
   retries where the pipeline was doing something else.

If the collector is not running, every ``profile_*`` tool returns
``profiling: disabled`` and succeeds. Profiling is observability: an agent must
never abandon a pipeline step because the observer is down.

Results in the session
----------------------

Each session gains a ``performance/`` directory, written live as the pipeline
runs:

.. code-block:: text

   workspaces/<app>/<run_id>/performance/
       performance_report.md     ← summary, per-step table, rework, slowest tools
       summary.json              ← whole-run profile snapshot
       steps/<n>-<step>.json     ← one file per pipeline step
       otlp/events-<date>.jsonl  ← raw telemetry events
       mlflow.json               ← experiment / parent-run / UI deep link

``session_final_report`` folds this into the deliverable as
``final_report/PERFORMANCE.md`` and ``final_report/performance/``, re-rendering
the report from ``summary.json`` so a stale report cannot ship.

Results in MLflow
-----------------

The same profile appears in MLflow (default ``http://localhost:5001``) as one
parent run per session, with one **nested run per pipeline step**. Each step run
carries ``cost_usd``, ``tokens_input`` / ``tokens_output`` /
``tokens_cache_read`` / ``tokens_cache_creation`` / ``tokens_total``, ``exec_s``,
``wall_s``, ``tries``, ``retries``, ``failed_attempts``,
``successful_attempts``, ``tool_calls``, ``tool_duration_ms`` and
``cost_usd_per_tool_call``.

Metrics are re-logged on a timer, so a running step shows a live cost curve and
its final point is its final value. A step that fails and is then retried moves
``FAILED`` → ``RUNNING`` → ``FINISHED``, so the MLflow UI and the report never
disagree about whether the pipeline succeeded.

Cost reconciliation
-------------------

The report sums ``cost_usd`` across ``api_request`` events, and independently
totals the ``claude_code.cost.usage`` counter. When the two disagree — because
log events were dropped or are still in flight — the report says so explicitly
rather than presenting an under-count as fact.
