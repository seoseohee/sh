"""ecc_core/registry.py — Probe/Verify 플러그인 레지스트리 (SWE-Agent ACI 패턴)."""

import os
import importlib.util
import pathlib


class _Registry:
    """런타임 등록 가능한 명령 레지스트리."""

    def __init__(self, base: dict):
        self._commands: dict[str, str] = dict(base)

    def register(self, name: str, command: str, overwrite: bool = False) -> None:
        if name in self._commands and not overwrite:
            raise ValueError(f"'{name}' already registered. Use overwrite=True to replace.")
        self._commands[name] = command

    def get(self, name: str) -> "str | None":
        return self._commands.get(name)

    def list_targets(self) -> list[str]:
        return sorted(self._commands.keys())

    def to_dict(self) -> dict:
        return dict(self._commands)


def _load_plugins(registry_type: str) -> dict:
    """ECC_PLUGIN_DIR 환경변수에서 외부 플러그인 로드."""
    plugin_dir = os.environ.get("ECC_PLUGIN_DIR", "")
    if not plugin_dir:
        return {}
    result = {}
    p = pathlib.Path(plugin_dir)
    if not p.is_dir():
        return {}
    for f in p.glob("*.py"):
        try:
            spec = importlib.util.spec_from_file_location(f.stem, f)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cmds = getattr(mod, registry_type, {})
            if isinstance(cmds, dict):
                result.update(cmds)
        except Exception:
            pass
    return result


def _make_registries() -> "tuple[_Registry, _Registry]":
    from .probe_commands  import PROBE_COMMANDS
    from .verify_commands import VERIFY_COMMANDS
    probe_plugins  = _load_plugins("PROBE_COMMANDS")
    verify_plugins = _load_plugins("VERIFY_COMMANDS")
    return (
        _Registry({**PROBE_COMMANDS,  **probe_plugins}),
        _Registry({**VERIFY_COMMANDS, **verify_plugins}),
    )


probe_registry, verify_registry = _make_registries()
