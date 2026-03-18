"""
ecc_core/tools.py — Backward-compatible re-export layer.

v4: tools.py split into 5 files.
  safety.py        — is_dangerous()
  probe_commands.py — PROBE_COMMANDS
  verify_commands.py — VERIFY_COMMANDS
  registry.py      — _Registry, probe_registry, verify_registry
  tool_schemas.py  — TOOL_DEFINITIONS, ASK_USER_TOOL, get_tool_definitions()

This file exists so existing code using 'from .tools import X'
continues to work — it's a thin re-export layer.
"""

from .safety          import DANGEROUS_PATTERNS, is_dangerous          # noqa: F401
from .probe_commands  import PROBE_COMMANDS                             # noqa: F401
from .verify_commands import VERIFY_COMMANDS                            # noqa: F401
from .registry        import _Registry, probe_registry, verify_registry # noqa: F401
from .tool_schemas    import TOOL_DEFINITIONS, ASK_USER_TOOL, get_tool_definitions  # noqa: F401
