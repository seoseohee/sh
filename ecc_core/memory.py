"""
ecc_core/memory.py

ECCMemory — 3-tier memory architecture (Sumers et al., TPAMI 2024)

수정 이력:
  v2 — dirty flag 기반 배치 저장.
       기존: remember() 호출마다 전체 JSON 디스크 write → probe 결과 10개
       저장 시 10회 I/O 발생.
       수정: _dirty flag로 변경 여부 추적, save() 는 _dirty일 때만 write.
       remember()는 즉시 디스크 저장 대신 dirty 표시 + flush_if_dirty() 제공.
       loop.py의 done() 및 세션 종료 시점에 save() 호출.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────
# Working Memory
# ─────────────────────────────────────────────────────────────

@dataclass
class WorkingMemory:
    goal: str = ""
    current_step: str = ""
    conn_address: str = ""
    turn: int = 0
    last_action: str = ""
    last_result: str = ""

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

@dataclass
class Episode:
    ts: float
    tool: str
    summary: str
    ok: bool

    @classmethod
    def from_result(cls, tool: str, result_text: str, ok: bool) -> "Episode":
        return cls(
            ts=time.time(),
            tool=tool,
            summary=result_text[:120].replace("\n", " "),
            ok=ok,
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
    3-tier 메모리 컨테이너.

    FIX v2: dirty flag 기반 배치 저장.
    - remember() 는 메모리에만 쓰고 _dirty = True 로 표시.
    - save() 는 _dirty 일 때만 디스크 write.
    - flush_if_dirty() 는 선택적 중간 저장용 (중요 사실 발견 후 즉시 보존).
    - 루프에서 done() 호출 시 및 KeyboardInterrupt 시 save() 호출.
    """

    def __init__(self, conn_address: str = ""):
        self.working = WorkingMemory()
        self.episodic: list[Episode] = []
        self.semantic = SemanticStore()
        self._conn_address = ""
        self._dirty = False

        if conn_address:
            self.update_connection(conn_address)

    # ── 연결 갱신 ──────────────────────────────────────────────

    def update_connection(self, conn_address: str) -> None:
        if self._conn_address == conn_address:
            return
        # 연결 변경 전 현재 dirty 데이터 저장
        if self._dirty and self._conn_address:
            self.save()
        self._conn_address = conn_address
        self.working.conn_address = conn_address
        self._load(conn_address)
        self._dirty = False

    # ── 영속 저장/로드 ─────────────────────────────────────────

    def _path(self, addr: str) -> Path:
        key = addr.replace("@", "_at_").replace(":", "_port_").replace("/", "_")
        p = Path(f"~/.ecc/memory/{key}.json").expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _checkpoint_path(self, addr: str) -> Path:
        """Working + Episodic 체크포인트 파일 경로."""
        key = addr.replace("@", "_at_").replace(":", "_port_").replace("/", "_")
        p = Path(f"~/.ecc/memory/{key}.checkpoint.json").expanduser()
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
        """
        Semantic memory를 디스크에 저장.
        FIX: _dirty가 False이면 불필요한 I/O를 건너뜀.
        """
        if not self._conn_address:
            return
        if not self._dirty:
            return
        try:
            self._path(self._conn_address).write_text(
                json.dumps(self.semantic.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._dirty = False
        except Exception:
            pass

    # ── 체크포인트 (Working + Episodic 휘발성 메모리 보존) ─────

    def checkpoint_save(self) -> None:
        """
        Working Memory + Episodic Memory를 디스크에 체크포인트 저장.

        SSH 단절 / 프로세스 종료 후 재시작 시 복원 가능하도록
        매 turn 종료 후 loop.py에서 호출한다.
        Semantic Memory와 별도 파일로 관리 (오염 방지).
        """
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
                        "ts":      e.ts,
                        "tool":    e.tool,
                        "summary": e.summary,
                        "ok":      e.ok,
                    }
                    for e in self.episodic[-50:]  # 최근 50개만 보존
                ],
            }
            self._checkpoint_path(self._conn_address).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def checkpoint_load(self) -> bool:
        """
        체크포인트 파일에서 Working + Episodic Memory 복원.

        Returns:
          True  — 복원 성공 (이전 세션 컨텍스트 있음)
          False — 체크포인트 없거나 로드 실패
        """
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
                    ts=e["ts"], tool=e["tool"],
                    summary=e["summary"], ok=e["ok"],
                )
                for e in data.get("episodic", [])
            ]
            return True
        except Exception:
            return False

    def checkpoint_clear(self) -> None:
        """
        체크포인트 삭제. done() 성공 시 호출 — 완료된 세션의 잔재 방지.
        """
        if not self._conn_address:
            return
        try:
            path = self._checkpoint_path(self._conn_address)
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def checkpoint_exists(self) -> bool:
        """체크포인트 파일 존재 여부."""
        if not self._conn_address:
            return False
        return self._checkpoint_path(self._conn_address).exists()

    def flush_if_dirty(self) -> None:
        """
        중요 사실 발견 직후 즉시 보존이 필요한 경우 명시적으로 호출.
        constraints, failed namespace 변경 시 권장.
        """
        if self._dirty:
            self.save()

    # ── 기록 헬퍼 ──────────────────────────────────────────────

    def remember(self, namespace: str, key: str, value) -> None:
        """
        Semantic memory에 사실 기록.

        FIX v2: 매 호출마다 디스크 write하지 않고 _dirty = True 표시.
        constraints / failed namespace는 즉시 flush (데이터 손실 위험이 높음).
        나머지는 배치 저장 (loop의 done() / 세션 종료 시).
        """
        self.semantic.set(namespace, key, value)
        self._dirty = True
        # 물리 제약과 실패 이력은 즉시 보존 — 세션 중단 시 재발견 비용이 크다
        if namespace in ("constraints", "failed"):
            self.flush_if_dirty()

    def record_episode(self, tool: str, result: str, ok: bool) -> None:
        self.episodic.append(Episode.from_result(tool, result, ok))
        if len(self.episodic) > 100:
            self.episodic = self.episodic[-100:]

    # ── 조회 헬퍼 ──────────────────────────────────────────────

    def to_system_context(self) -> str:
        parts = []
        wm = self.working.to_context()
        if wm:
            parts.append(wm)
        failed_eps = [e for e in self.episodic[-10:] if not e.ok][-3:]
        if failed_eps:
            ep_lines = ["[Recent failures]"]
            for e in failed_eps:
                ep_lines.append(f"  {e.tool}: {e.summary[:80]}")
            parts.append("\n".join(ep_lines))
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
        hw = self.semantic.hardware

        if "ros2" in hint and not hw.get("ros2_available"):
            return False, "ROS2 not confirmed on board — run probe(target='sw') first"
        if "serial" in hint and not hw.get("serial_ports"):
            return False, "No serial devices confirmed — run probe(target='hw') first"
        if "lidar" in hint and not hw.get("lidar_device"):
            return False, "No LiDAR confirmed — run probe(target='lidar') first"

        return True, ""

    def update_working(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self.working, key):
                setattr(self.working, key, value)

    # ── typed semantic remember helpers ───────────────────────────

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
