"""
ecc_core/tools.py — 하위 호환 re-export 레이어.

v4: tools.py를 5개 파일로 분리.
  safety.py        — is_dangerous()
  probe_commands.py — PROBE_COMMANDS
  verify_commands.py — VERIFY_COMMANDS
  registry.py      — _Registry, probe_registry, verify_registry
  tool_schemas.py  — TOOL_DEFINITIONS, ASK_USER_TOOL, get_tool_definitions()

이 파일은 기존 코드가 'from .tools import X' 형태로 임포트하는 것을
깨지 않기 위한 thin re-export 레이어.
"""

from .safety          import DANGEROUS_PATTERNS, is_dangerous          # noqa: F401
from .probe_commands  import PROBE_COMMANDS                             # noqa: F401
from .verify_commands import VERIFY_COMMANDS                            # noqa: F401
from .registry        import _Registry, probe_registry, verify_registry # noqa: F401
from .tool_schemas    import TOOL_DEFINITIONS, ASK_USER_TOOL, get_tool_definitions  # noqa: F401
