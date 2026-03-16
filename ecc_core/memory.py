"""
ecc_core/memory.py

ECCMemory — 3-tier memory architecture (Sumers et al., TPAMI 2024)

  Working Memory  — 현재 실행 컨텍스트 (세션 내 빠른 읽기/쓰기)
  Episodic Memory — 이번 세션의 시간순 사건 기록
  Semantic Memory — 보드별 영속 지식 (세션 간 ~/.ecc/memory/ 에 보존)
    .hardware    ← 디바이스 경로, ROS2 토픽, 환경 (Hardware Memory)
    .protocol    ← baud rate, QoS, 통신 설정 (Protocol Memory)
    .skill       ← 재사용 가능한 검증된 스크립트 (Skill Memory)
    .constraints ← 물리 한계 (min_erpm, max_temp 등)
    .failed      ← 실패한 접근법 (재시도 방지)

BoardMemory.update_connection() 으로 보드별 영속 파일 로드.
세션 간 재연결 후에도 하드웨어 사실/제약/실패 이력 유지.
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
    last_action: str = ""   # 직전 tool 이름
    last_result: str = ""   # 직전 결과 요약 (50자)

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
# Episodic Memory — 시간순 사건 기록
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
# Semantic Memory — namespace 기반 영속 지식 저장소
# ─────────────────────────────────────────────────────────────

class SemanticStore:
    """
    6개 memory 타입을 namespace로 통합:
      hardware    ← Hardware Memory
      protocol    ← Protocol Memory
      skill       ← Skill Memory
      constraints ← 물리 제약 (압축 후에도 반드시 보존)
      failed      ← 실패 이력 (재시도 방지)
    """

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
        """system 프롬프트에 주입할 요약. 압축 후에도 살아남는 핵심 정보."""
        lines = []
        count = 0
        # constraints와 failed를 우선 표시
        for ns in ("constraints", "failed", "hardware", "protocol", "skill"):
            for k, v in list(self._d.get(ns, {}).items())[:3]:
                if count >= max_items:
                    break
                lines.append(f"  [{ns}] {k} = {str(v)[:100]}")
                count += 1
        if not lines:
            return ""
        return "[Board memory — do not rediscover]\n" + "\n".join(lines)

    # 편의 프로퍼티
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
# ECCMemory — 3 tier 컨테이너
# ─────────────────────────────────────────────────────────────

class ECCMemory:
    """
    Layer C 아키텍처의 핵심 상태 컨테이너.

    - Working: 현재 turn 컨텍스트
    - Episodic: 이번 세션 사건 기록
    - Semantic: 보드별 영속 지식 (~/.ecc/memory/<board>.json)
    """

    def __init__(self, conn_address: str = ""):
        self.working = WorkingMemory()
        self.episodic: list[Episode] = []
        self.semantic = SemanticStore()
        self._conn_address = ""

        if conn_address:
            self.update_connection(conn_address)

    # ── 연결 갱신 ──────────────────────────────────────────────

    def update_connection(self, conn_address: str) -> None:
        """SSH 연결 후 호출. 보드별 영속 메모리 로드."""
        if self._conn_address == conn_address:
            return
        self._conn_address = conn_address
        self.working.conn_address = conn_address
        self._load(conn_address)

    # ── 영속 저장/로드 ─────────────────────────────────────────

    def _path(self, addr: str) -> Path:
        key = addr.replace("@", "_at_").replace(":", "_port_").replace("/", "_")
        p = Path(f"~/.ecc/memory/{key}.json").expanduser()
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
        """Semantic memory를 디스크에 영속 저장."""
        if not self._conn_address:
            return
        try:
            self._path(self._conn_address).write_text(
                json.dumps(self.semantic.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── 기록 헬퍼 ──────────────────────────────────────────────

    def remember(self, namespace: str, key: str, value) -> None:
        """Semantic memory에 사실 기록 + 즉시 디스크 저장."""
        self.semantic.set(namespace, key, value)
        self.save()

    def record_episode(self, tool: str, result: str, ok: bool) -> None:
        """Episodic memory에 사건 추가. 최근 100개만 유지."""
        self.episodic.append(Episode.from_result(tool, result, ok))
        if len(self.episodic) > 100:
            self.episodic = self.episodic[-100:]

    # ── 조회 헬퍼 ──────────────────────────────────────────────

    def to_system_context(self) -> str:
        """loop.py system_with_state에 추가할 컨텍스트 문자열."""
        parts = []
        wm = self.working.to_context()
        if wm:
            parts.append(wm)
        # 최근 실패 에피소드 3개 — escalation이 집계하기 전 패턴을 LLM이 직접 인지
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
        """
        compactor.py가 압축 후에도 보존할 사실 문자열.
        constraints와 failed는 반드시 포함.
        """
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
        """
        SayCan-style affordance check — Task Graph 없이.
        semantic.hardware 기반으로 실행 가능성 검사.
        """
        hint = task_hint.lower()
        hw = self.semantic.hardware

        if "ros2" in hint and not hw.get("ros2_available"):
            return False, "ROS2 not confirmed on board — run probe(target='sw') first"
        if "serial" in hint and not hw.get("serial_ports"):
            return False, "No serial devices confirmed — run probe(target='hw') first"
        if "lidar" in hint and not hw.get("lidar_device"):
            return False, "No LiDAR confirmed — run probe(target='lidar') first"

        return True, ""

    # ── typed working memory updater ──────────────────────────────

    def update_working(self, **kwargs) -> None:
        """WorkingMemory 필드를 키워드 인자로 일괄 업데이트.

        직접 속성 접근 대신 이 메서드를 사용하면
        존재하지 않는 필드를 조용히 무시해서 런타임 오류를 방지.
        예: memory.update_working(current_step="모터 속도 확인", turn=3)
        """
        for key, value in kwargs.items():
            if hasattr(self.working, key):
                setattr(self.working, key, value)

    # ── typed semantic remember helpers ───────────────────────────
    # remember("namespace", key, value) 보다 오타가 없고 IDE 자동완성 지원

    def remember_hardware(self, key: str, value) -> None:
        """hardware namespace에 저장. 예: motor_topic, serial_ports."""
        self.remember("hardware", key, value)

    def remember_protocol(self, key: str, value) -> None:
        """protocol namespace에 저장. 예: baud_rate, qos."""
        self.remember("protocol", key, value)

    def remember_constraint(self, key: str, value) -> None:
        """constraints namespace에 저장. 예: min_erpm, max_speed_ms."""
        self.remember("constraints", key, value)

    def remember_failed_approach(self, key: str, value) -> None:
        """failed namespace에 저장. 재시도 방지용.
        예: remember_failed_approach("pub_once_loop", "ARG_MAX timeout")
        """
        self.remember("failed", key, value)

    def remember_skill(self, key: str, value) -> None:
        """skill namespace에 저장. 재사용 가능한 검증 스크립트."""
        self.remember("skill", key, value)
