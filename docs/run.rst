Running the MCP server
======================

.. tip::

   To start the MCP server together with the profiling collector and MLflow in
   one idempotent command, use ``dftracer_agents_stack start``. See
   :doc:`profiling`. The sections below cover running the MCP server on its own.

Stdio mode
----------

The default transport is stdio:

.. code-block:: bash

   dftracer-mcp-server

This mode is appropriate for harnesses that launch the server as a child
process and discover tools over stdio.

HTTP mode
---------

The server also supports streamable HTTP:

.. code-block:: bash

   dftracer-mcp-server --transport http --host 0.0.0.0 --port 5000 --path /mcp

The default path is ``/mcp`` and the default port is ``5000``.

Client configuration helpers
----------------------------

To configure Claude Code and Goose for the HTTP endpoint, run:

.. code-block:: bash

   dftracer-configure-mcp --host localhost --port 5000 --path /mcp

Use ``--no-claude`` or ``--no-goose`` to skip one client, and ``--dry-run``
to print the changes without writing any files.
