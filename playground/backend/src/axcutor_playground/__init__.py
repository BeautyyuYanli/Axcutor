"""Execution HTTP adapters and a local workbench built on Axcutor public APIs.

The default workbench owns workspace storage and one resident Worker's lifecycle.
The independent API owns neither. Host and runtime remain independent of HTTP;
Temporal owns execution state in both modes.
"""
