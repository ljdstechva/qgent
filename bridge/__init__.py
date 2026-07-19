# -*- coding: utf-8 -*-
"""Execution bridge: socket server + main-thread executor.

The stdlib-only MCP stdio bridge (``mcp_stdio_bridge.py``) is intentionally NOT
imported from here — it runs as a separate process launched by the CLI and must
not pull in any QGIS/Qt modules.
"""
