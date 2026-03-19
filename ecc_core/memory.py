"""
ecc_core/memory.py — ECCMemory 3-tier (v6)

Changelog:
  v2 — dirty flag batch save
  v3 — checkpoint (Working + Episodic volatile preservation)
  v4 — Episode importance + causal chain + Semantic query search + SSH profile cache
  v5 — env overrides for all constants
  v6 — [Improve D] SemanticEntry: TTL / confidence / source 필드 추가.
         기존: SemanticStore가 단순 dict라 "언제 저장됐는지", "얼마나 신뢰할 수 있는지",
               "언제 만료되는지" 정보가 없어 오래된/무효화된 제약이 영구 잔류.
         수정:
           - SemanticEntry 데이터클래스: value + created_at + confidence + source + expires_at
           - remember()에 confidence/source/ttl_seconds 인자 추가
           - constraints 네임스페이스는 기본 ttl=86400(24h) 적용
           - is_valid() 로 만료 체크, prune_expired()로 일괄 정리
           - get_ssh_profile() 등 기존 API 그대로 유지 (값만 꺼내서 반환)
         [Improve D2] 기존 직렬화(JSON) 하위호환: 구버전 plain value도 로드 가능.
"""

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any


def _mem_int(key: str, default: int) -> int:
    try: return int(os.environ.get(key, default))
    except (ValueError, TypeError): return default

def _mem_float(key: str, default: float) -> float:
    try: return float(os.environ.get(key, default))
    except (ValueError, TypeError): return default


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
        if self.goal:         parts.append(f"Goal: {self.goal[:200]}")
        if self.current_step: parts.append(f"Current step: {self.current_step[:100]}")
        if self.conn_address: parts.append(f"Connected: {self.conn_address}")
        if self.last_action:  parts.append(f"Last action: {self.last_action} → {self.last_result[:50]}")
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
    def from_result(cls, tool, result_text, ok, turn=0, caused_by="") -> "Episode":
        base = _TOOL_IMPORTANCE.get(tool, 0.4)
        importance = min(1.0, base + (0.2 if not ok else 0.0))
        if any(k in result_text for k in ("/dev/", "PASS", "connected", "found")):
            importance = min(1.0, importance + 0.15)
        return cls(ts=time.time(), tool=tool, summary=result_text[:120].replace("\n", " "),
                   ok=ok, importance=importance, caused_by=caused_by, turn=turn)

    def id(self) -> str:
        return f"{self.tool}:{self.summary[:30]}"

    def recency_score(self, current_turn: int) -> float:
        delta = max(0, current_turn - self.turn)
        return _recency_decay() ** delta

    def relevance_score(self, query: str) -> float:
        q_words = set(query.lower().split())
        e_words = set((self.tool + " " + self.summary).lower().split())
        if not q_words: return 0.0
        return min(1.0, len(q_words & e_words) / max(1, len(q_words)) * 2)

    def retrieval_score(self, query: str, current_turn: int) -> float:
        return (self.recency_score(current_turn) * self.importance
                * max(0.1, self.relevance_score(query)))


# ─────────────────────────────────────────────────────────────
# [Improve D] SemanticEntry — TTL / confidence / source
# ─────────────────────────────────────────────────────────────

# constraints 네임스페이스 기본 TTL (초). 0 = 영구.
_CONSTRAINTS_DEFAULT_TTL = 86_400  # 24시간

def _constraints_ttl() -> int:
    """ECC_CONSTRAINTS_TTL 환경변수로 오버라이드 가능."""
    return _mem_int("ECC_CONSTRAINTS_TTL", _CONSTRAINTS_DEFAULT_TTL)


@dataclass
class SemanticEntry:
    """Semantic Memory 단일 항목.

    value       : 저장된 값 (str, int, float, list, dict 모두 가능)
    created_at  : Unix timestamp
    confidence  : 0.0~1.0. remember() 직접 저장=1.0, 추론/통합=0.6
    source      : "direct" | "inferred" | "consolidated"
    expires_at  : None=영구, Unix timestamp=만료 시각

    직렬화 시 {"__entry__": true, ...} 형식으로 저장해 구버전 plain value와 구분.
    """
    value:      Any
    created_at: float  = field(default_factory=time.time)
    confidence: float  = 1.0
    source:     str    = "direct"
    expires_at: Optional[float] = None

    def is_valid(self) -> bool:
        """만료되지 않았으면 True."""
        if self.expires_at is None:
            return True
        return time.time() < self.expires_at

    def to_dict(self) -> dict:
        return {
            "__entry__":  True,
            "value":      self.value,
            "created_at": self.created_at,
            "confidence": self.confidence,
            "source":     self.source,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SemanticEntry":
        return cls(
            value      = d["value"],
            created_at = d.get("created_at", time.time()),
            confidence = d.get("confidence", 1.0),
            source     = d.get("source", "direct"),
            expires_at = d.get("expires_at"),
        )

    @classmethod
    def from_raw(cls, raw) -> "SemanticEntry":
        """구버전 plain value 또는 신버전 dict 모두 처리."""
        if isinstance(raw, dict) and raw.get("__entry__"):
            return cls.from_dict(raw)
        return cls(value=raw)


# ─────────────────────────────────────────────────────────────
# Semantic Memory Store
# ─────────────────────────────────────────────────────────────

class SemanticStore:
    NAMESPACES = ("hardware", "protocol", "skill", "constraints", "failed")

    def __init__(self, data: Optional[dict] = None):
        # 내부: {ns: {key: SemanticEntry}}
        self._d: dict[str, dict[str, SemanticEntry]] = {ns: {} for ns in self.NAMESPACES}
        if data:
            for ns in self.NAMESPACES:
                if ns in data:
                    for k, v in data[ns].items():
                        self._d[ns][k] = SemanticEntry.from_raw(v)

    def set(self, ns: str, key: str, value,
            confidence: float = 1.0,
            source: str = "direct",
            ttl_seconds: Optional[int] = None) -> None:
        """항목 저장.

        [Improve D] constraints 네임스페이스는 기본 TTL 적용.
        ttl_seconds=0 또는 None이면 영구 저장.
        """
        expires_at = None
        if ttl_seconds is None and ns == "constraints":
            ttl = _constraints_ttl()
            if ttl > 0:
                expires_at = time.time() + ttl
        elif ttl_seconds and ttl_seconds > 0:
            expires_at = time.time() + ttl_seconds

        self._d.setdefault(ns, {})[key] = SemanticEntry(
            value=value, created_at=time.time(),
            confidence=confidence, source=source, expires_at=expires_at,
        )

    def get(self, ns: str, key: str, default=None):
        """유효한 항목의 value를 반환. 만료됐으면 default."""
        entry = self._d.get(ns, {}).get(key)
        if entry is None or not entry.is_valid():
            return default
        return entry.value

    def get_entry(self, ns: str, key: str) -> Optional[SemanticEntry]:
        """SemanticEntry 전체 반환. 만료됐으면 None."""
        entry = self._d.get(ns, {}).get(key)
        if entry is None or not entry.is_valid():
            return None
        return entry

    def ns(self, namespace: str) -> dict:
        """유효한 항목만 {key: value} 형태로 반환."""
        return {k: e.value for k, e in self._d.get(namespace, {}).items() if e.is_valid()}

    def prune_expired(self) -> int:
        """만료된 항목 삭제. 삭제된 개수 반환."""
        count = 0
        for ns_dict in self._d.values():
            expired = [k for k, e in ns_dict.items() if not e.is_valid()]
            for k in expired:
                del ns_dict[k]
                count += 1
        return count

    def to_dict(self) -> dict:
        """직렬화. SemanticEntry.to_dict() 형식으로 저장."""
        result = {}
        for ns, ns_dict in self._d.items():
            result[ns] = {k: e.to_dict() for k, e in ns_dict.items()}
        return result

    def to_prompt_context(self, max_items: int = 10) -> str:
        lines = []
        count = 0
        for ns in ("constraints", "failed", "hardware", "protocol", "skill"):
            for k, v in list(self.ns(ns).items())[:3]:
                if count >= max_items: break
                lines.append(f"  [{ns}] {k} = {str(v)[:100]}")
                count += 1
        if not lines: return ""
        return "[Board memory — do not rediscover]\n" + "\n".join(lines)

    def query_relevant(self, query: str, top_k: int = None) -> str:
        if top_k is None:
            top_k = _mem_int("ECC_SEMANTIC_TOP_K", 6)
        q_words = set(query.lower().split())
        scored = []
        for ns in ("constraints", "failed"):
            for k, v in self.ns(ns).items():
                scored.append((10.0, ns, k, v))
        for ns in ("hardware", "protocol", "skill"):
            for k, v in self.ns(ns).items():
                text = (k + " " + str(v)).lower()
                overlap = sum(1 for w in q_words if w in text)
                if overlap > 0:
                    scored.append((float(overlap), ns, k, v))
        scored.sort(key=lambda x: -x[0])
        if not scored: return ""
        lines = [f"  [{ns}] {k} = {str(v)[:100]}" for _, ns, k, v in scored[:top_k]]
        return "[Board memory — relevant to current task]\n" + "\n".join(lines)

    # ── 프로퍼티 (하위 호환) ────────────────────────────────────
    @property
    def hardware(self) -> dict:    return self.ns("hardware")
    @property
    def protocol(self) -> dict:    return self.ns("protocol")
    @property
    def skill(self) -> dict:       return self.ns("skill")
    @property
    def constraints(self) -> dict: return self.ns("constraints")
    @property
    def failed(self) -> dict:      return self.ns("failed")


# ─────────────────────────────────────────────────────────────
# ECCMemory
# ─────────────────────────────────────────────────────────────

class ECCMemory:
    """3-tier memory container v6."""

    def __init__(self, conn_address: str = ""):
        self.working   = WorkingMemory()
        self.episodic: list[Episode] = []
        self.semantic  = SemanticStore()
        self._conn_address = ""
        self._dirty    = False
        self._last_episode_id: str = ""

        if conn_address:
            self.update_connection(conn_address)

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
                }, source="direct", ttl_seconds=0)  # SSH 프로파일은 영구
                self._dirty = True

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
                # 로드 직후 만료 항목 정리
                pruned = self.semantic.prune_expired()
                if pruned > 0:
                    print(f"  🗑️  {pruned} expired semantic entries pruned on load", flush=True)
            except Exception:
                pass

    def save(self) -> None:
        if not self._conn_address or not self._dirty:
            return
        try:
            self.semantic.prune_expired()  # 저장 전 만료 항목 정리
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
        if not self._conn_address: return
        ep_max = _mem_int("ECC_CHECKPOINT_EPISODIC_MAX", 50)
        try:
            data = {
                "working": {
                    "goal": self.working.goal, "current_step": self.working.current_step,
                    "conn_address": self.working.conn_address, "turn": self.working.turn,
                    "last_action": self.working.last_action, "last_result": self.working.last_result,
                },
                "episodic": [
                    {"ts": e.ts, "tool": e.tool, "summary": e.summary, "ok": e.ok,
                     "importance": e.importance, "caused_by": e.caused_by, "turn": e.turn}
                    for e in self.episodic[-ep_max:]
                ],
            }
            self._checkpoint_path(self._conn_address).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except Exception:
            pass

    def checkpoint_load(self) -> bool:
        if not self._conn_address: return False
        path = self._checkpoint_path(self._conn_address)
        if not path.exists(): return False
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
                Episode(ts=e["ts"], tool=e["tool"], summary=e["summary"], ok=e["ok"],
                        importance=e.get("importance", 0.5), caused_by=e.get("caused_by", ""),
                        turn=e.get("turn", 0))
                for e in data.get("episodic", [])
            ]
            return True
        except Exception:
            return False

    def checkpoint_clear(self) -> None:
        if not self._conn_address: return
        try:
            path = self._checkpoint_path(self._conn_address)
            if path.exists(): path.unlink()
        except Exception:
            pass

    def checkpoint_exists(self) -> bool:
        if not self._conn_address: return False
        return self._checkpoint_path(self._conn_address).exists()

    # ── Recording helpers ──────────────────────────────────────

    def remember(self, namespace: str, key: str, value,
                 confidence: float = 1.0,
                 source: str = "direct",
                 ttl_seconds: Optional[int] = None) -> None:
        """[Improve D] confidence / source / ttl_seconds 인자 추가.
        constraints 네임스페이스는 ttl_seconds 미지정 시 기본 TTL(_constraints_ttl()) 적용.
        """
        self.semantic.set(namespace, key, value,
                          confidence=confidence, source=source, ttl_seconds=ttl_seconds)
        self._dirty = True
        if namespace in ("constraints", "failed"):
            self.flush_if_dirty()

    def record_episode(self, tool: str, result: str, ok: bool) -> None:
        ep = Episode.from_result(tool=tool, result_text=result, ok=ok,
                                 turn=self.working.turn, caused_by=self._last_episode_id)
        self.episodic.append(ep)
        self._last_episode_id = ep.id()
        ep_max = _mem_int("ECC_EPISODIC_MAX", 100)
        if len(self.episodic) > ep_max:
            self.episodic = self.episodic[-ep_max:]

    # ── Search helpers ─────────────────────────────────────────

    def retrieve_episodes(self, query: str, top_k: int = None,
                          failed_only: bool = False) -> list[Episode]:
        if top_k is None:
            top_k = _mem_int("ECC_EPISODIC_TOP_K", 5)
        pool = [e for e in self.episodic if not failed_only or not e.ok]
        if not pool: return []
        return sorted(pool, key=lambda e: e.retrieval_score(query, self.working.turn),
                      reverse=True)[:top_k]

    def to_system_context(self, query: str = "") -> str:
        parts = []
        wm = self.working.to_context()
        if wm: parts.append(wm)

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
                parts.append("[Recent failures]\n" + "\n".join(
                    f"  {e.tool}: {e.summary[:80]}" for e in failed_eps))

        sm = self.semantic.query_relevant(query) if query else self.semantic.to_prompt_context()
        if sm: parts.append(sm)
        return "\n\n".join(parts)

    def get_persistent_facts(self) -> str:
        lines = []
        for ns in ("constraints", "hardware", "protocol"):
            for k, v in self.semantic.ns(ns).items():
                lines.append(f"{ns}.{k} = {v}")
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

    # ── 편의 메서드 ────────────────────────────────────────────

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

    def remember_constraint(self, key: str, value,
                            ttl_seconds: Optional[int] = None) -> None:
        """[Improve D] constraints는 기본 TTL 적용. 명시적으로 ttl_seconds=0 하면 영구."""
        self.remember("constraints", key, value, ttl_seconds=ttl_seconds)

    def remember_failed_approach(self, key: str, value) -> None:
        self.remember("failed", key, value)

    def remember_skill(self, key: str, value) -> None:
        self.remember("skill", key, value, ttl_seconds=0)  # 스킬은 항상 영구