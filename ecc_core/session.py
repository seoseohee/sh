from dataclasses import dataclass, field
# Explicit followup prefixes — anything else is treated as a new goal
#
# [Fix 5] /more / /add / /also 제거.
#   이 접두어들은 임베디드 명령/일반 목표에서도 자연스럽게 쓰여 오판 가능.
#   예) "/also check temperature" → 새 goal이지만 followup으로 처리됨
#   /continue / /resume만 유지: 명확하게 "이전 세션 재개" 의도가 있는 경우만 허용.
_FOLLOWUP_PREFIXES = ("/continue", "/resume")
@dataclass
class SessionState:
    messages: list = field(default_factory=list); goal: str = ""
    todos: object = None; executor: object = None; memory: object = None
class SessionManager:
    def __init__(self):
        self._saved_messages = []; self._saved_goal = ""; self._saved_todos = None
        self._saved_executor = None; self._saved_memory = None
    @staticmethod
    def is_followup(goal, has_session):
        if not has_session: return False
        return any(goal.strip().lower().startswith(p) for p in _FOLLOWUP_PREFIXES)
    def has_saved_session(self): return bool(self._saved_messages)
    def init_session(self, goal, conn, verbose):
        from ecc_core.todo import TodoManager
        from ecc_core.executor import ToolExecutor
        from ecc_core.memory import ECCMemory
        followup = self.is_followup(goal, self.has_saved_session())
        active_goal = self._saved_goal if followup else goal
        memory = (self._saved_memory if (followup and self._saved_memory) else ECCMemory(conn_address=conn.address if conn else ""))
        memory.working.goal = active_goal
        todos = TodoManager(); executor = ToolExecutor(conn, todos, memory=memory, verbose=verbose)
        messages = [{"role":"user","content":goal}]
        return SessionState(messages=messages,goal=active_goal,todos=todos,executor=executor,memory=memory), followup
    def save(self, state):
        self._saved_messages = list(state.messages); self._saved_goal = state.goal
        self._saved_todos = state.todos; self._saved_executor = state.executor; self._saved_memory = state.memory
        if state.memory: state.memory.save()
    def save_partial(self,messages,goal,todos,executor,memory):
        if messages:
            self._saved_messages=list(messages); self._saved_goal=goal
            self._saved_todos=todos; self._saved_executor=executor; self._saved_memory=memory
            if memory: memory.save()
    def reset(self):
        self._saved_messages=[]; self._saved_goal=""; self._saved_todos=None; self._saved_executor=None; self._saved_memory=None
    def _current_state_snapshot(self): return None
    @property
    def saved_messages(self): return self._saved_messages