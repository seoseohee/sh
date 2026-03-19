"""
ecc_core/memory.py — ECCMemory 3-tier (v4)

Changelog:
  v2 — dirty flag batch save
  v3 — checkpoint (Working + Episodic volatile preservation)
  v4 — 4 research-based improvements
       1. Episode importance score + recency×importance×relevance search
       2. Episodic causal chain (caused_by link)
       3. Semantic query-based filter search (retrieve_relevant)
       4. SSH profile caching
  v5 — [env overrides] 하드코딩 상수 환경변수 오버라이드 추가
         ECC_EPISODIC_MAX            : 에피소드 최대 저장 수 (기본 100)
         ECC_CHECKPOINT_EPISODIC_MAX : 체크포인트에 저장하는 에피소드 수 (기본 50)
         ECC_RECENCY_DECAY           : 에피소드 감쇠율 0~1 (기본 0.995)
         ECC_SEMANTIC_TOP_K          : Semantic 검색 결과 수 (기본 6)
         ECC_EPISODIC_TOP_K          : 에피소드 검색 결과 수 (기본 5)
         ECC_CONTEXT_EPISODIC_TOP_K  : 컨텍스트 주입 실패 에피소드 수 (기본 3)
         ECC_PERSISTENT_FAILED_MAX   : get_persistent_facts failed 표시 수 (기본 6)
"""

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _mem_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

def _mem_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


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
# Episodic Memory
# ─────────────────────────────────────────────────────────────

_TOOL_IMPORTANCE: dict[str, float] = {
    "ssh_connect":  0.9,
    "probe":        0.8,
    "verify":       0.8,
    "remember":     0.9,
    "done":         1.0,
    "serial_open":  0.7,
    "script":       0.5,
    "bash":         0.3,
    "todo":         0.2,
}

# [env override] ECC_RECENCY_DECAY (기본 0.995)
def _recency_decay() -> float:
    return _mem_float("ECC_RECENCY_DECAY", 0.995)


@dataclass
class Episode:
    ts:         float
    tool:       str
    summary:    str
    ok:         bool
    importance: float = 0.5
    caused_by:  str   = ""
    turn:       int   = 0

    @classmethod
    def from_result(
        cls,
        tool:        str,
        result_text: str,
        ok:          bool,
        turn:        int = 0,
        caused_by:   str = "",
    ) -> "Episode":
        base = _TOOL_IMPORTANCE.get(tool, 0.4)
        importance = min(1.0, base + (0.2 if not ok else 0.0))
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
        return f"{self.tool}:{self.summary[:30]}"

    def recency_score(self, current_turn: int) -> float:
        delta = max(0, current_turn - self.turn)
        return _recency_decay() ** delta

    def relevance_score(self, query: str) -> float:
        q_words = set(query.lower().split())
        e_words = set((self.tool + " " + self.summary).lower().split())
        if not q_words:
            return 0.0
        overlap = len(q_words & e_words)
        return min(1.0, overlap / max(1, len(q_words)) * 2)

    def retrieval_score(self, query: str, current_turn: int) -> float:
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

    def query_relevant(self, query: str, top_k: int = None) -> str:
        """query-relevant Semantic memory 필터 반환."""
        # [env override] ECC_SEMANTIC_TOP_K (기본 6)
        if top_k is None:
            top_k = _mem_int("ECC_SEMANTIC_TOP_K", 6)

        q_words = set(query.lower().split())
        scored: list[tuple[float, str, str, object]] = []

        for ns in ("constraints", "failed"):
            for k, v in self._d.get(ns, {}).items():
                scored.append((10.0, ns, k, v))

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
    """3-tier memory container v5."""

    def __init__(self, conn_address: str = ""):
        self.working   = WorkingMemory()
        self.episodic: list[Episode] = []
        self.semantic  = SemanticStore()
        self._conn_address = ""
        self._dirty    = False
        self._last_episode_id: str = ""

        if conn_address:
            self.update_connection(conn_address)

    # ── Connection update ──────────────────────────────────────

    def update_connection(self, conn_address: str) -> None:
        if self._conn_address == conn_address:
            return
        if self._dirty and self._conn_address:
            self.save()
        self._conn_address = conn_address
        self.working.conn_address = conn_address
        self._load(conn_address)
        self._dirty = False

        if conn_address:
            parts = conn_address.split("@")
            if len(parts) == 2:
                self.semantic.set("hardware", "ssh_profile", {
                    "user":           parts[0],
                    "host_port":      parts[1],
                    "last_connected": time.time(),
                })
                self._dirty = True

    # ── Persistent save/load ──────────────────────────────────

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

    # ── Checkpoint ────────────────────────────────────────────

    def checkpoint_save(self) -> None:
        if not self._conn_address:
            return
        # [env override] ECC_CHECKPOINT_EPISODIC_MAX (기본 50)
        ep_max = _mem_int("ECC_CHECKPOINT_EPISODIC_MAX", 50)
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
                    for e in self.episodic[-ep_max:]
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

    # ── Recording helpers ──────────────────────────────────────

    def remember(self, namespace: str, key: str, value) -> None:
        self.semantic.set(namespace, key, value)
        self._dirty = True
        if namespace in ("constraints", "failed"):
            self.flush_if_dirty()

    def record_episode(self, tool: str, result: str, ok: bool) -> None:
        ep = Episode.from_result(
            tool=tool, result_text=result, ok=ok,
            turn=self.working.turn, caused_by=self._last_episode_id,
        )
        self.episodic.append(ep)
        self._last_episode_id = ep.id()

        # [env override] ECC_EPISODIC_MAX (기본 100)
        ep_max = _mem_int("ECC_EPISODIC_MAX", 100)
        if len(self.episodic) > ep_max:
            self.episodic = self.episodic[-ep_max:]

    # ── Search helpers ─────────────────────────────────────────

    def retrieve_episodes(
        self,
        query:       str,
        top_k:       int  = None,
        failed_only: bool = False,
    ) -> list[Episode]:
        """Generative Agents 공식 기반 에피소드 검색."""
        # [env override] ECC_EPISODIC_TOP_K (기본 5)
        if top_k is None:
            top_k = _mem_int("ECC_EPISODIC_TOP_K", 5)

        pool = [e for e in self.episodic if not failed_only or not e.ok]
        if not pool:
            return []
        current_turn = self.working.turn
        return sorted(
            pool,
            key=lambda e: e.retrieval_score(query, current_turn),
            reverse=True,
        )[:top_k]

    def to_system_context(self, query: str = "") -> str:
        parts = []

        wm = self.working.to_context()
        if wm:
            parts.append(wm)

        # [env override] ECC_CONTEXT_EPISODIC_TOP_K (기본 3)
        ctx_top_k = _mem_int("ECC_CONTEXT_EPISODIC_TOP_K", 3)

        if query and self.episodic:
            relevant_fails = self.retrieve_episodes(query, top_k=ctx_top_k, failed_only=True)
            if relevant_fails:
                ep_lines = ["[Relevant failures]"]
                for e in relevant_fails:
                    caused = f" ← {e.caused_by[:30]}" if e.caused_by else ""
                    ep_lines.append(f"  {e.tool}: {e.summary[:80]}{caused}")
                parts.append("\n".join(ep_lines))
        else:
            failed_eps = [e for e in self.episodic[-10:] if not e.ok][-ctx_top_k:]
            if failed_eps:
                ep_lines = ["[Recent failures]"]
                for e in failed_eps:
                    ep_lines.append(f"  {e.tool}: {e.summary[:80]}")
                parts.append("\n".join(ep_lines))

        if query:
            sm = self.semantic.query_relevant(query)
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

        # [env override] ECC_PERSISTENT_FAILED_MAX (기본 6)
        failed_max = _mem_int("ECC_PERSISTENT_FAILED_MAX", 6)
        failed = self.semantic.ns("failed")
        if failed:
            lines.append("--- Do NOT retry (previously failed) ---")
            for k, v in list(failed.items())[:failed_max]:
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

    def get_ssh_profile(self) -> "dict | None":
        return self.semantic.get("hardware", "ssh_profile")

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