"""
ecc_core/todo.py

Changelog:
  v2 — [Improve F] TodoItem 의존성 그래프 지원.
         기존: flat 목록으로 순서/병렬 여부 정보 없음.
         수정:
           - depends_on: list[str]  — 선행 태스크 id 목록
           - estimated_turns: int   — LLM 예측 소요 턴 (선택)
           - ready_items()          — depends_on의 모든 선행 태스크가 completed인 pending 항목
           - parallel_candidates()  — 동시 실행 가능한 ready 항목 집합
         하위호환: depends_on/estimated_turns 없는 기존 todo dict도 정상 처리.
"""
from dataclasses import dataclass, field
from typing import Literal

Status   = Literal["pending", "in_progress", "completed"]
Priority = Literal["high", "medium", "low"]


@dataclass
class TodoItem:
    id:              str
    content:         str
    status:          Status   = "pending"
    priority:        Priority = "medium"
    # [Improve F]
    depends_on:      list[str] = field(default_factory=list)
    estimated_turns: int       = 1


class TodoManager:
    STATUS_ICONS   = {"pending": "○", "in_progress": "→", "completed": "✓"}
    PRIORITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}

    def __init__(self):
        self._todos: list[TodoItem] = []

    def update(self, raw_todos: list[dict]):
        self._todos = [
            TodoItem(
                id              = t.get("id", f"t{i}"),
                content         = t.get("content", ""),
                status          = t.get("status", "pending"),
                priority        = t.get("priority", "medium"),
                depends_on      = t.get("depends_on", []),        # [Improve F]
                estimated_turns = t.get("estimated_turns", 1),    # [Improve F]
            )
            for i, t in enumerate(raw_todos)
        ]

    def has_todos(self) -> bool:
        return bool(self._todos)

    def all_completed(self) -> bool:
        return bool(self._todos) and all(t.status == "completed" for t in self._todos)

    def in_progress_items(self) -> list[TodoItem]:
        return [t for t in self._todos if t.status == "in_progress"]

    # ── [Improve F] 의존성 인식 쿼리 ──────────────────────────

    def completed_ids(self) -> set[str]:
        return {t.id for t in self._todos if t.status == "completed"}

    def ready_items(self) -> list[TodoItem]:
        """선행 태스크가 모두 완료된 pending 항목 반환 (실행 가능 상태).

        depends_on이 비어있거나, 명시된 모든 id가 completed면 ready.
        """
        done = self.completed_ids()
        return [
            t for t in self._todos
            if t.status == "pending"
            and all(dep in done for dep in t.depends_on)
        ]

    def parallel_candidates(self) -> list[TodoItem]:
        """동시 실행 가능한 ready 항목들.

        ready 항목 중 서로 의존성이 없는 것들을 반환.
        현재 in_progress 항목과 depends_on으로 얽히지 않은 것만 포함.

        dispatcher.py에서 이 목록을 보고 병렬 서브에이전트 디스패치 여부 결정 가능.
        """
        ready = self.ready_items()
        in_progress_ids = {t.id for t in self.in_progress_items()}

        # in_progress 항목을 depends_on으로 참조하는 ready 항목 제외
        # (이미 실행 중인 것이 끝나야 시작 가능한 것들)
        candidates = [
            t for t in ready
            if not any(dep in in_progress_ids for dep in t.depends_on)
        ]
        return candidates

    def dependency_summary(self) -> str:
        """의존성 현황 요약 문자열 (system prompt 주입용)."""
        ready = self.ready_items()
        blocked = [
            t for t in self._todos
            if t.status == "pending" and t not in ready
        ]
        if not ready and not blocked:
            return ""
        lines = []
        if ready:
            lines.append(f"[Ready to execute: {', '.join(t.id for t in ready)}]")
        if blocked:
            lines.append(f"[Blocked (waiting for deps): {', '.join(t.id for t in blocked)}]")
        return "\n".join(lines)

    # ── 포맷 ──────────────────────────────────────────────────

    def format_display(self) -> str:
        if not self._todos:
            return ""
        lines = ["  📋 Progress:"]
        for t in self._todos:
            s   = self.STATUS_ICONS.get(t.status, "?")
            p   = self.PRIORITY_ICONS.get(t.priority, "")
            dep = f" ← [{','.join(t.depends_on)}]" if t.depends_on else ""
            lines.append(f"    {s} {p} [{t.id}] {t.content}{dep}")
        return "\n".join(lines)

    def format_for_llm(self) -> str:
        if not self._todos:
            return "(no todos)"
        lines = []
        for t in self._todos:
            dep = f" | depends_on=[{','.join(t.depends_on)}]" if t.depends_on else ""
            lines.append(f"[{t.id}] {t.status} | {t.content}{dep}")
        return "\n".join(lines)

    def format_nag(self) -> str:
        remaining = [t for t in self._todos if t.status != "completed"]
        if not remaining:
            return ""
        lines = ["[Currently in progress]"]
        for t in remaining:
            s   = self.STATUS_ICONS.get(t.status, "?")
            dep = f" (needs: {','.join(t.depends_on)})" if t.depends_on and t.status == "pending" else ""
            lines.append(f"  {s} [{t.id}] {t.content}{dep}")
        # 의존성 요약 추가
        dep_summary = self.dependency_summary()
        if dep_summary:
            lines.append(dep_summary)
        return "\n".join(lines)