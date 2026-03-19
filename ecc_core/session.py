"""
ecc_core/session.py

Changelog:
  v2 — [Fix] _current_state_snapshot() 항상 None 반환 버그 수정.
         기존: 메서드가 return None으로 고정되어 KeyboardInterrupt 시
               _save_partial_session()이 아무것도 저장하지 않음.
         수정: set_running()으로 루프 매 턴 상태를 갱신,
               _current_state_snapshot()이 실제 상태를 반환.
       [Refactor] 한 줄 압축 코드 → 정상 포맷.
"""

from dataclasses import dataclass, field

# Explicit followup prefixes — anything else is treated as a new goal.
# /continue / /resume 만 유지: "이전 세션 재개" 의도가 명확한 경우만 허용.
_FOLLOWUP_PREFIXES = ("/continue", "/resume")


@dataclass
class SessionState:
    messages: list  = field(default_factory=list)
    goal:     str   = ""
    todos:    object = None
    executor: object = None
    memory:   object = None


class SessionManager:
    def __init__(self):
        self._saved_messages: list  = []
        self._saved_goal:     str   = ""
        self._saved_todos           = None
        self._saved_executor        = None
        self._saved_memory          = None
        # [Fix] mid-loop 상태 — KeyboardInterrupt partial-save 용
        self._running: dict | None  = None

    # ── Followup detection ──────────────────────────────────────

    @staticmethod
    def is_followup(goal: str, has_session: bool) -> bool:
        if not has_session:
            return False
        return any(goal.strip().lower().startswith(p) for p in _FOLLOWUP_PREFIXES)

    def has_saved_session(self) -> bool:
        return bool(self._saved_messages)

    # ── Session init ────────────────────────────────────────────

    def init_session(
        self,
        goal:    str,
        conn,
        verbose: bool,
    ) -> tuple[SessionState, bool]:
        from ecc_core.todo     import TodoManager
        from ecc_core.executor import ToolExecutor
        from ecc_core.memory   import ECCMemory

        followup    = self.is_followup(goal, self.has_saved_session())
        active_goal = self._saved_goal if followup else goal

        memory = (
            self._saved_memory
            if (followup and self._saved_memory)
            else ECCMemory(conn_address=conn.address if conn else "")
        )
        memory.working.goal = active_goal

        todos    = TodoManager()
        executor = ToolExecutor(conn, todos, memory=memory, verbose=verbose)
        messages = [{"role": "user", "content": goal}]

        return SessionState(
            messages=messages,
            goal=active_goal,
            todos=todos,
            executor=executor,
            memory=memory,
        ), followup

    # ── Mid-loop state tracking (for partial-save on interrupt) ─

    def set_running(
        self,
        messages: list,
        goal:     str,
        todos,
        executor,
        memory,
    ) -> None:
        """매 턴 끝에 루프에서 호출. KeyboardInterrupt 시 partial-save 재료."""
        self._running = dict(
            messages=messages,
            goal=goal,
            todos=todos,
            executor=executor,
            memory=memory,
        )

    def _current_state_snapshot(self) -> tuple | None:
        """
        [Fix] 기존 구현은 return None 고정이었음.

        loop.py의 _save_partial_session()이 이 메서드를 호출해
        save_partial()에 언패킹해 넘기는데, None이 반환되면 아무것도
        저장되지 않아 KeyboardInterrupt 후 세션이 항상 유실됨.

        set_running()으로 갱신된 최신 상태를 반환하도록 수정.
        """
        if not self._running:
            return None
        r = self._running
        return r["messages"], r["goal"], r["todos"], r["executor"], r["memory"]

    # ── Persistence ─────────────────────────────────────────────

    def save(self, state: SessionState) -> None:
        """done() 후 정상 종료 시 호출."""
        self._saved_messages = list(state.messages)
        self._saved_goal     = state.goal
        self._saved_todos    = state.todos
        self._saved_executor = state.executor
        self._saved_memory   = state.memory
        self._running        = None          # mid-loop 상태 초기화
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
        """KeyboardInterrupt 등 비정상 종료 시 호출."""
        if not messages:
            return
        self._saved_messages = list(messages)
        self._saved_goal     = goal
        self._saved_todos    = todos
        self._saved_executor = executor
        self._saved_memory   = memory
        if memory:
            memory.save()

    def reset(self) -> None:
        self._saved_messages = []
        self._saved_goal     = ""
        self._saved_todos    = None
        self._saved_executor = None
        self._saved_memory   = None
        self._running        = None

    # ── Properties (backward compat for cli.py) ─────────────────

    @property
    def saved_messages(self) -> list:
        return self._saved_messages