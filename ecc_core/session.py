"""ecc_core/session.py — 세션 상태 관리.

AgentLoop에서 세션 관련 책임을 분리:
  - followup 판정 (_is_followup)
  - 세션 상태 초기화 (새 goal vs 이어받기)
  - 부분 세션 저장 (_save_partial_session)
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .todo     import TodoManager
    from .executor import ToolExecutor
    from .memory   import ECCMemory


# 명시적 followup 접두어 — 이 외는 전부 새 goal로 처리
_FOLLOWUP_PREFIXES = ("/continue", "/resume", "/more", "/add", "/also")


@dataclass
class SessionState:
    """현재 실행 중인 세션의 공유 상태."""
    messages:   list[dict]         = field(default_factory=list)
    goal:       str                = ""
    todos:      "TodoManager | None"  = None
    executor:   "ToolExecutor | None" = None
    memory:     "ECCMemory | None"    = None


class SessionManager:
    """세션 초기화 + 부분 저장 담당."""

    def __init__(self):
        self._saved_messages:  list[dict]         = []
        self._saved_goal:      str                = ""
        self._saved_todos:     "TodoManager | None"  = None
        self._saved_executor:  "ToolExecutor | None" = None
        self._saved_memory:    "ECCMemory | None"    = None

    @staticmethod
    def is_followup(goal: str, has_session: bool) -> bool:
        """명시적 접두어 기반 followup 판정.

        단어 수 휴리스틱을 사용하지 않는다.
        '1m/s로 주행' 같은 짧은 새 goal이 followup으로 오판되는 버그 방지.
        """
        if not has_session:
            return False
        stripped = goal.strip()
        return any(stripped.lower().startswith(p) for p in _FOLLOWUP_PREFIXES)

    def has_saved_session(self) -> bool:
        return bool(self._saved_messages)

    def init_session(
        self,
        goal: str,
        conn,
        verbose: bool,
    ) -> "tuple[SessionState, bool]":
        """
        goal을 받아 SessionState를 초기화한다.

        Returns:
          (state, is_followup)
        """
        from .todo     import TodoManager
        from .executor import ToolExecutor
        from .memory   import ECCMemory

        followup    = self.is_followup(goal, self.has_saved_session())
        active_goal = self._saved_goal if followup else goal

        memory = (
            self._saved_memory
            if (followup and self._saved_memory)
            else ECCMemory(conn_address=conn.address if conn else "")
        )
        memory.working.goal = active_goal

        if followup:
            todos    = self._saved_todos or TodoManager()
            executor = self._saved_executor or ToolExecutor(conn, todos, memory=memory, verbose=verbose)
            executor.conn     = conn
            executor.memory   = memory
            executor.is_finished = False
            messages = self._saved_messages + [
                {"role": "user", "content": f"[User follow-up] {goal}"}
            ]
        else:
            todos    = TodoManager()
            executor = ToolExecutor(conn, todos, memory=memory, verbose=verbose)
            messages = [{"role": "user", "content": goal}]

        state = SessionState(
            messages=messages,
            goal=active_goal,
            todos=todos,
            executor=executor,
            memory=memory,
        )
        return state, followup

    def save(self, state: "SessionState") -> None:
        """세션 종료 시 상태 보존."""
        self._saved_messages = list(state.messages)
        self._saved_goal     = state.goal
        self._saved_todos    = state.todos
        self._saved_executor = state.executor
        self._saved_memory   = state.memory
        if state.memory:
            state.memory.save()

    def save_partial(
        self,
        messages,
        goal,
        todos,
        executor,
        memory,
    ) -> None:
        """KeyboardInterrupt 등 중단 시 부분 저장."""
        if messages:
            self._saved_messages = list(messages)
            self._saved_goal     = goal
            self._saved_todos    = todos
            self._saved_executor = executor
            self._saved_memory   = memory
            if memory:
                memory.save()

    def reset(self) -> None:
        """REPL /reset 명령 대응."""
        self._saved_messages = []
        self._saved_goal     = ""
        self._saved_todos    = None
        self._saved_executor = None
        self._saved_memory   = None

    def _current_state_snapshot(self) -> "tuple | None":
        """loop.py의 _save_partial_session()용 내부 스냅샷."""
        if not self._saved_messages:
            return None
        return (
            self._saved_messages,
            self._saved_goal,
            self._saved_todos,
            self._saved_executor,
            self._saved_memory,
        )

    @property
    def saved_messages(self) -> list[dict]:
        return self._saved_messages
