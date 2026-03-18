"""
ecc_core/memory.py — ECCMemory 3-tier (v4)

Changelog:
  v2 — dirty flag batch save
  v3 — checkpoint (Working + Episodic volatile preservation)
  v4 — 4 research-based improvements
       1. Episode importance score + recency×importance×relevance search
       2. Episodic causal chain (caused_by link)
       3. Semantic query-based filter search (retrieve_relevant)
       4. SSH profile caching (hardware.ssh_profile → faster reconnect)

References:
  - Generative Agents (Park et al., 2023): recency×importance×relevance formula
  - MAGMA (Jiang et al., 2026): causal graph representation
  - AgeMem (Yu et al., 2026): memory ops as agent tools
"""

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────
# Working Memory
# ─────────────────────────────────────────────────────────────

@dataclass
class WorkingMemory:
    goal:         str = ""
    current_step: str = ""
    conn_address: str = ""
    turn:         int = 0
    last_action:  str = ""
    last_result:  str = ""

    def to_context(self) -> str:
        parts = []
        if self.goal:
            parts.append(f"Goal: {self.goal[:200]}")
        if self.current_step:
            parts.append(f"Current step: {self.current_step[:100]}")
        if self.conn_address:
            parts.append(f"Connected: {self.conn_address}")
        if self.last_action:
            parts.append(f"Last action: {self.last_action} → {self.last_result[:50]}")
        return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# Episodic Memory — importance + causal chain (v4)
# ─────────────────────────────────────────────────────────────

# Base importance per tool (0~1)
_TOOL_IMPORTANCE: dict[str, float] = {
    "ssh_connect":  0.9,   # connection event — always important
    "probe":        0.8,   # hardware discovery
    "verify":       0.8,   # verification result
    "remember":     0.9,   # explicit save = important
    "done":         1.0,   # session end
    "serial_open":  0.7,
    "script":       0.5,
    "bash":         0.3,   # general command — low
    "todo":         0.2,   # state update — low
}
_RECENCY_DECAY = 0.995     # per-turn decay (0.995^100 ≈ 0.6)


@dataclass
class Episode:
    ts:         float
    tool:       str
    summary:    str
    ok:         bool
    importance: float = 0.5   # 0~1
    caused_by:  str   = ""    # causal link: prev episode ID (tool:summary[:30])
    turn:       int   = 0     # Turn number when this occurred

    @classmethod
    def from_result(
        cls,
        tool:       str,
        result_text: str,
        ok:         bool,
        turn:       int  = 0,
        caused_by:  str  = "",
    ) -> "Episode":
        # base importance per tool
        base = _TOOL_IMPORTANCE.get(tool, 0.4)
        # failures get +0.2 importance (more important for retry prevention)
        importance = min(1.0, base + (0.2 if not ok else 0.0))
        # first discovery (hw path, IP etc.) gets extra boost
        if any(k in result_text for k in ("/dev/", "PASS", "connected", "found")):
            importance = min(1.0, importance + 0.15)

        return cls(
            ts=time.time(),
            tool=tool,
            summary=result_text[:120].replace("\n", " "),
            ok=ok,
            importance=importance,
            caused_by=caused_by,
            turn=turn,
        )

    def id(self) -> str:
        """Identifier for causal links."""
        return f"{self.tool}:{self.summary[:30]}"

    def recency_score(self, current_turn: int) -> float:
        """Recency score relative to current turn (decay function)."""
        delta = max(0, current_turn - self.turn)
        return _RECENCY_DECAY ** delta

    def relevance_score(self, query: str) -> float:
        """Keyword overlap-based relevance score (0~1)."""
        q_words = set(query.lower().split())
        e_words = set((self.tool + " " + self.summary).lower().split())
        if not q_words:
            return 0.0
        overlap = len(q_words & e_words)
        return min(1.0, overlap / max(1, len(q_words)) * 2)

    def retrieval_score(self, query: str, current_turn: int) -> float:
        """Generative Agents formula: recency × importance × relevance."""
        return (
            self.recency_score(current_turn)
            * self.importance
            * max(0.1, self.relevance_score(query))
        )


# ─────────────────────────────────────────────────────────────
# Semantic Memory
# ─────────────────────────────────────────────────────────────

class SemanticStore:
    NAMESPACES = ("hardware", "protocol", "skill", "constraints", "failed")

    def __init__(self, data: Optional[dict] = None):
        self._d: dict = {ns: {} for ns in self.NAMESPACES}
        if data:
            for ns in self.NAMESPACES:
                if ns in data:
                    self._d[ns] = data[ns]

    def set(self, ns: str, key: str, value) -> None:
        self._d.setdefault(ns, {})[key] = value

    def get(self, ns: str, key: str, default=None):
        return self._d.get(ns, {}).get(key, default)

    def ns(self, namespace: str) -> dict:
        return self._d.get(namespace, {})

    def to_dict(self) -> dict:
        return {k: dict(v) for k, v in self._d.items()}

    def to_prompt_context(self, max_items: int = 10) -> str:
        lines = []
        count = 0
        for ns in ("constraints", "failed", "hardware", "protocol", "skill"):
            for k, v in list(self._d.get(ns, {}).items())[:3]:
                if count >= max_items:
                    break
                lines.append(f"  [{ns}] {k} = {str(v)[:100]}")
                count += 1
        if not lines:
            return ""
        return "[Board memory — do not rediscover]\n" + "\n".join(lines)

    def query_relevant(self, query: str, top_k: int = 6) -> str:
        """
        Filter and return only query-relevant Semantic memory.

        v4: selective injection based on keyword overlap instead of full dump.
        Implements proposal 4 (query-based Semantic search).
        """
        q_words = set(query.lower().split())
        scored: list[tuple[float, str, str, object]] = []

        # constraints/failed always included first
        for ns in ("constraints", "failed"):
            for k, v in self._d.get(ns, {}).items():
                scored.append((10.0, ns, k, v))  # top priority

        for ns in ("hardware", "protocol", "skill"):
            for k, v in self._d.get(ns, {}).items():
                text = (k + " " + str(v)).lower()
                overlap = sum(1 for w in q_words if w in text)
                if overlap > 0:
                    scored.append((float(overlap), ns, k, v))

        scored.sort(key=lambda x: -x[0])
        if not scored:
            return ""
        lines = [f"  [{ns}] {k} = {str(v)[:100]}" for _, ns, k, v in scored[:top_k]]
        return "[Board memory — relevant to current task]\n" + "\n".join(lines)

    @property
    def hardware(self) -> dict:
        return self._d["hardware"]

    @property
    def protocol(self) -> dict:
        return self._d["protocol"]

    @property
    def skill(self) -> dict:
        return self._d["skill"]

    @property
    def constraints(self) -> dict:
        return self._d["constraints"]

    @property
    def failed(self) -> dict:
        return self._d["failed"]


# ─────────────────────────────────────────────────────────────
# ECCMemory
# ─────────────────────────────────────────────────────────────

class ECCMemory:
    """
    3-tier memory container v4.

    Working  — Current turn context (volatile)
    Episodic — Session event log (importance+causality+search)
    Semantic — Board-specific persistent knowledge (dirty flag, checkpoint)
    """

    def __init__(self, conn_address: str = ""):
        self.working   = WorkingMemory()
        self.episodic: list[Episode] = []
        self.semantic  = SemanticStore()
        self._conn_address = ""
        self._dirty    = False
        self._last_episode_id: str = ""   # for causal chain linking

        if conn_address:
            self.update_connection(conn_address)

    # ── Connection update ──────────────────────────────────────────────

    def update_connection(self, conn_address: str) -> None:
        if self._conn_address == conn_address:
            return
        if self._dirty and self._conn_address:
            self.save()
        self._conn_address = conn_address
        self.working.conn_address = conn_address
        self._load(conn_address)
        self._dirty = False

        # v4: SSH profile caching — auto-save on successful connection
        if conn_address:
            parts = conn_address.split("@")
            if len(parts) == 2:
                user_part = parts[0]
                host_port = parts[1]
                self.semantic.set("hardware", "ssh_profile", {
                    "user": user_part,
                    "host_port": host_port,
                    "last_connected": time.time(),
                })
                self._dirty = True

    # ── Persistent save/load ─────────────────────────────────────────

    def _path(self, addr: str) -> Path:
        key = addr.replace("@", "_at_").replace(":", "_port_").replace("/", "_")
        p   = Path(f"~/.ecc/memory/{key}.json").expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _checkpoint_path(self, addr: str) -> Path:
        key = addr.replace("@", "_at_").replace(":", "_port_").replace("/", "_")
        p   = Path(f"~/.ecc/memory/{key}.checkpoint.json").expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _load(self, addr: str) -> None:
        path = self._path(addr)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.semantic = SemanticStore(data)
            except Exception:
                pass

    def save(self) -> None:
        if not self._conn_address or not self._dirty:
            return
        try:
            self._path(self._conn_address).write_text(
                json.dumps(self.semantic.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._dirty = False
        except Exception:
            pass

    def flush_if_dirty(self) -> None:
        if self._dirty:
            self.save()

    # ── Checkpoint (Working + Episodic) ───────────────────────

    def checkpoint_save(self) -> None:
        if not self._conn_address:
            return
        try:
            data = {
                "working": {
                    "goal":         self.working.goal,
                    "current_step": self.working.current_step,
                    "conn_address": self.working.conn_address,
                    "turn":         self.working.turn,
                    "last_action":  self.working.last_action,
                    "last_result":  self.working.last_result,
                },
                "episodic": [
                    {
                        "ts":         e.ts,
                        "tool":       e.tool,
                        "summary":    e.summary,
                        "ok":         e.ok,
                        "importance": e.importance,
                        "caused_by":  e.caused_by,
                        "turn":       e.turn,
                    }
                    for e in self.episodic[-50:]
                ],
            }
            self._checkpoint_path(self._conn_address).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def checkpoint_load(self) -> bool:
        if not self._conn_address:
            return False
        path = self._checkpoint_path(self._conn_address)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            w = data.get("working", {})
            self.working.goal         = w.get("goal", "")
            self.working.current_step = w.get("current_step", "")
            self.working.conn_address = w.get("conn_address", "")
            self.working.turn         = w.get("turn", 0)
            self.working.last_action  = w.get("last_action", "")
            self.working.last_result  = w.get("last_result", "")
            self.episodic = [
                Episode(
                    ts=e["ts"], tool=e["tool"], summary=e["summary"],
                    ok=e["ok"], importance=e.get("importance", 0.5),
                    caused_by=e.get("caused_by", ""), turn=e.get("turn", 0),
                )
                for e in data.get("episodic", [])
            ]
            return True
        except Exception:
            return False

    def checkpoint_clear(self) -> None:
        if not self._conn_address:
            return
        try:
            path = self._checkpoint_path(self._conn_address)
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def checkpoint_exists(self) -> bool:
        if not self._conn_address:
            return False
        return self._checkpoint_path(self._conn_address).exists()

    # ── Recording helpers ──────────────────────────────────────────────

    def remember(self, namespace: str, key: str, value) -> None:
        self.semantic.set(namespace, key, value)
        self._dirty = True
        if namespace in ("constraints", "failed"):
            self.flush_if_dirty()

    def record_episode(self, tool: str, result: str, ok: bool) -> None:
        """
        Record episode (v4: importance + causal chain).
        """
        ep = Episode.from_result(
            tool=tool,
            result_text=result,
            ok=ok,
            turn=self.working.turn,
            caused_by=self._last_episode_id,
        )
        self.episodic.append(ep)
        self._last_episode_id = ep.id()
        if len(self.episodic) > 100:
            self.episodic = self.episodic[-100:]

    # ── Search helpers ──────────────────────────────────────────────

    def retrieve_episodes(
        self,
        query:        str,
        top_k:        int  = 5,
        failed_only:  bool = False,
    ) -> list[Episode]:
        """
        v4: Episode search based on Generative Agents formula.
        Returns top-k by recency × importance × relevance.
        """
        pool = [e for e in self.episodic if not failed_only or not e.ok]
        if not pool:
            return []
        current_turn = self.working.turn
        scored = sorted(
            pool,
            key=lambda e: e.retrieval_score(query, current_turn),
            reverse=True,
        )
        return scored[:top_k]

    def to_system_context(self, query: str = "") -> str:
        """
        v4: added query parameter.
        With query: inject only relevant Semantic items and relevant failures from Episodic.
        Without query: old behavior (full dump).
        """
        parts = []

        wm = self.working.to_context()
        if wm:
            parts.append(wm)

        # Episodic: query-based search or fallback to recent failures
        if query and self.episodic:
            relevant_fails = self.retrieve_episodes(query, top_k=3, failed_only=True)
            if relevant_fails:
                ep_lines = ["[Relevant failures]"]
                for e in relevant_fails:
                    caused = f" ← {e.caused_by[:30]}" if e.caused_by else ""
                    ep_lines.append(f"  {e.tool}: {e.summary[:80]}{caused}")
                parts.append("\n".join(ep_lines))
        else:
            failed_eps = [e for e in self.episodic[-10:] if not e.ok][-3:]
            if failed_eps:
                ep_lines = ["[Recent failures]"]
                for e in failed_eps:
                    ep_lines.append(f"  {e.tool}: {e.summary[:80]}")
                parts.append("\n".join(ep_lines))

        # Semantic: query-based filter or full dump
        if query:
            sm = self.semantic.query_relevant(query, top_k=6)
        else:
            sm = self.semantic.to_prompt_context()
        if sm:
            parts.append(sm)

        return "\n\n".join(parts)

    def get_persistent_facts(self) -> str:
        lines = []
        for ns in ("constraints", "hardware", "protocol"):
            for k, v in self.semantic.ns(ns).items():
                lines.append(f"{ns}.{k} = {v}")
        failed = self.semantic.ns("failed")
        if failed:
            lines.append("--- Do NOT retry (previously failed) ---")
            for k, v in list(failed.items())[:6]:
                lines.append(f"  FAILED: {k} → {str(v)[:80]}")
        return "\n".join(lines)

    def can_execute(self, task_hint: str) -> tuple[bool, str]:
        hint = task_hint.lower()
        hw   = self.semantic.hardware
        if "ros2" in hint and not hw.get("ros2_available"):
            return False, "ROS2 not confirmed — run probe(target='sw') first"
        if "serial" in hint and not hw.get("serial_ports"):
            return False, "No serial devices confirmed — run probe(target='hw') first"
        if "lidar" in hint and not hw.get("lidar_device"):
            return False, "No LiDAR confirmed — run probe(target='lidar') first"
        return True, ""

    def update_working(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self.working, key):
                setattr(self.working, key, value)

    # ── SSH profile cache helper (v4) ──────────────────────────

    def get_ssh_profile(self) -> "dict | None":
        """
        Return previously successful SSH connection info.
        Try first in BoardDiscovery.from_hint() to reduce discovery time.
        """
        return self.semantic.get("hardware", "ssh_profile")

    # ── typed remember helpers ──────────────────────────────

    def remember_hardware(self, key: str, value) -> None:
        self.remember("hardware", key, value)

    def remember_protocol(self, key: str, value) -> None:
        self.remember("protocol", key, value)

    def remember_constraint(self, key: str, value) -> None:
        self.remember("constraints", key, value)

    def remember_failed_approach(self, key: str, value) -> None:
        self.remember("failed", key, value)

    def remember_skill(self, key: str, value) -> None:
        self.remember("skill", key, value)
