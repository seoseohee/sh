"""ecc_core/session.py — Session state management.

Session-related responsibilities separated from AgentLoop:
  - followup detection (_is_followup)
  - session state init (new goal vs resume)
  - partial session save (_save_partial_session)
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .todo     import TodoManager
    from .executor import ToolExecutor
    from .memory   import ECCMemory


# Explicit followup prefixes — anything else is treated as a new goal
_FOLLOWUP_PREFIXES = ("/continue", "/resume", "/more", "/add", "/also")


@dataclass
class SessionState:
    """Shared state for the currently running session."""
    messages:   list[dict]         = field(default_factory=list)
    goal:       str                = ""
    todos:      "TodoManager | None"  = None
    executor:   "ToolExecutor | None" = None
    memory:     "ECCMemory | None"    = None


class SessionManager:
    """Handles session init + partial save."""

    def __init__(self):
        self._saved_messages:  list[dict]         = []
        self._saved_goal:      str                = ""
        self._saved_todos:     "TodoManager | None"  = None
        self._saved_executor:  "ToolExecutor | None" = None
        self._saved_memory:    "ECCMemory | None"    = None

    @staticmethod
    def is_followup(goal: str, has_session: bool) -> bool:
        """Followup detection based on explicit prefixes.

        Word count heuristics are not used.
        Prevents short new goals (e.g. 'drive at 1m/s') from being misclassified as followups.
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
        Initialize SessionState from goal.

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
        """Preserve state on session end."""
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
        """Partial save on KeyboardInterrupt."""
        if messages:
            self._saved_messages = list(messages)
            self._saved_goal     = goal
            self._saved_todos    = todos
            self._saved_executor = executor
            self._saved_memory   = memory
            if memory:
                memory.save()

    def reset(self) -> None:
        """Handle REPL /reset command."""
        self._saved_messages = []
        self._saved_goal     = ""
        self._saved_todos    = None
        self._saved_executor = None
        self._saved_memory   = None

    def _current_state_snapshot(self) -> "tuple | None":
        """Internal snapshot for loop.py _save_partial_session()."""
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
