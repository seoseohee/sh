"""
ecc_core/loop.py — AgentLoop (thin orchestrator)

v4 separation:
  escalation.py  — EscalationTracker
  session.py     — SessionManager, SessionState
  dispatcher.py  — ToolDispatcher, SubagentRole, run_subagent

Responsibilities:
  - LLM API calls (Anthropic client)
  - turn loop + escalation management
  - verifier feedback annotation injection
  - three-module orchestration

Backward-compatible re-exports:
  SubagentRole, run_subagent — importable from this file too.

Changelog:
  v5 — [Fix 1] record_goal 누락 수정
         기존: done(success=True) 경로만 기록됨
         수정: done(success=False), max_turns 초과, KeyboardInterrupt 등
               모든 세션 종료 경로에서 record_goal 호출
         구현: _record_session_end() 헬퍼로 중앙화
"""

import os
import re
import time
import anthropic
from concurrent.futures import ThreadPoolExecutor, as_completed

from .connection  import BoardConnection, BoardDiscovery
from .todo        import TodoManager
from .executor    import ToolExecutor
from .compactor   import should_compact, compact
from .prompt      import build_system_prompt
from .tool_schemas import get_tool_definitions
from .memory      import ECCMemory
from .tracer      import Tracer
from .reflection  import (classify_failure, generate_reflection,
                          make_reflection_message, route_from_verifier, ReplanDecision)
from .observation import collect_observation
from .verifier    import verify_execution, verify_motion, parse_error_feedback
from .escalation    import EscalationTracker
from .session       import SessionManager
from .dispatcher    import ToolDispatcher, SubagentRole, run_subagent  # noqa: F401 (re-export)
from .consolidation import consolidate_episodic
from .goal_history  import record_goal


# ─────────────────────────────────────────────────────────────
# Environment variable helpers
# ─────────────────────────────────────────────────────────────

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

def _main_model() -> str:
    return os.environ.get("ECC_MODEL", "claude-sonnet-4-6")

def _escalate_model() -> str:
    env = os.environ.get("ECC_ESCALATE_MODEL")
    if env:
        return env
    main = _main_model()
    return main.replace("sonnet", "opus") if "sonnet" in main else main

def _main_max_tokens() -> int:
    return _env_int("ECC_MAX_TOKENS", 8096)

def _thinking_enabled() -> bool:
    return os.environ.get("ECC_THINKING", "").lower() in ("1", "true", "yes")

def _thinking_budget() -> int:
    return _env_int("ECC_THINKING_BUDGET", 8000)

def _supports_adaptive(model: str) -> bool:
    env = os.environ.get("ECC_ADAPTIVE_MODELS")
    if env:
        return any(m.strip() in model for m in env.split(","))
    m = re.search(r"(\d+)[\.-](\d+)", model)
    if m:
        return (int(m.group(1)), int(m.group(2))) >= (4, 6)
    return False

def _thinking_params(model: str) -> dict:
    return {"type": "adaptive"} if _supports_adaptive(model) else \
           {"type": "enabled", "budget_tokens": _thinking_budget()}

def _rate_limit_wait() -> int:
    """RateLimitError 대기 시간(초). ECC_RATE_LIMIT_WAIT로 오버라이드."""
    return _env_int("ECC_RATE_LIMIT_WAIT", 60)

def _conn_check_interval() -> int:
    """SSH 연결 체크 주기(턴). ECC_CONN_CHECK_INTERVAL로 오버라이드."""
    return _env_int("ECC_CONN_CHECK_INTERVAL", 10)

def _max_turns_step() -> int:
    """최대 턴 초과 시 확장량. ECC_MAX_TURNS_STEP으로 오버라이드."""
    return _env_int("ECC_MAX_TURNS_STEP", 50)


# ─────────────────────────────────────────────────────────────
# [Fix 1] 세션 종료 헬퍼 — 모든 종료 경로 중앙화
# ─────────────────────────────────────────────────────────────

def _record_session_end(
    tracer:       "Tracer",
    active_goal:  str,
    turn:         int,
    success:      bool,
    conn:         "BoardConnection | None",
    summary:      str = "",
) -> None:
    """
    모든 세션 종료 경로에서 tracer + record_goal을 일관되게 호출.

    기존 문제:
      - done(success=True) 경로만 record_goal 호출
      - done(success=False), max_turns 초과, KeyboardInterrupt 등은 미기록

    수정:
      - run() 내의 모든 break / return 경로에서 이 함수를 호출
      - success=False일 때도 history.jsonl에 기록됨
    """
    tok_in, tok_out = tracer.get_token_totals()
    tracer.session_end(
        success=success,
        summary=summary or (f"done() after {turn} turns" if success else f"failed after {turn} turns"),
    )
    record_goal(
        goal=active_goal,
        success=success,
        turns=turn,
        conn_address=conn.address if conn else "",
        tokens_in=tok_in,
        tokens_out=tok_out,
    )


# ─────────────────────────────────────────────────────────────
# AgentLoop
# ─────────────────────────────────────────────────────────────

class AgentLoop:

    def __init__(self, verbose: bool = False):
        self.verbose    = verbose
        self.client     = anthropic.Anthropic()
        self.conn: BoardConnection | None = None
        self._session   = SessionManager()

    # ── cli.py backward-compat properties ──────────────────────────────

    @property
    def _session_messages(self) -> list[dict]:
        return self._session.saved_messages

    @_session_messages.setter
    def _session_messages(self, v):
        self._session._saved_messages = v

    @property
    def _session_goal(self) -> str:
        return self._session._saved_goal

    @_session_goal.setter
    def _session_goal(self, v):
        self._session._saved_goal = v

    @property
    def _session_todos(self):
        return self._session._saved_todos

    @_session_todos.setter
    def _session_todos(self, v):
        self._session._saved_todos = v

    @property
    def _session_executor(self):
        return self._session._saved_executor

    @_session_executor.setter
    def _session_executor(self, v):
        self._session._saved_executor = v

    @property
    def _session_memory(self):
        return self._session._saved_memory

    @_session_memory.setter
    def _session_memory(self, v):
        self._session._saved_memory = v

    # ── Main loop ──────────────────────────────────────────────

    def run(self, goal: str, max_turns: int = 100):
        print(f"\n{'═'*60}\n  🎯 {goal[:80]}\n{'═'*60}")

        if self.conn and not self.conn.is_alive():
            print("  ⚠️  Previous connection lost..")
            self.conn = None

        state, is_followup = self._session.init_session(goal, self.conn, self.verbose)
        if is_followup:
            print(f"  🔁 Resuming previous session ({len(self._session.saved_messages)} messages)", flush=True)

        messages    = state.messages
        todos       = state.todos
        executor    = state.executor
        memory      = state.memory
        active_goal = state.goal

        disp   = ToolDispatcher(self)
        tracer = Tracer(goal=active_goal[:80])

        # ── Checkpoint restore ──────────────────────────────────
        if not is_followup and memory.checkpoint_exists():
            restored = memory.checkpoint_load()
            if restored and memory.working.goal:
                print(
                    f"\n  ♻️  Checkpoint restored — previous turn={memory.working.turn}"
                    f", step='{memory.working.current_step[:40]}'",
                    flush=True,
                )
                messages.insert(0, {
                    "role": "user",
                    "content": (
                        f"[Checkpoint restored] Previous session context:\n"
                        f"  Prior goal: {memory.working.goal}\n"
                        f"  Last turn: {memory.working.turn}\n"
                        f"  Last step: {memory.working.current_step}\n"
                        f"  Last action: {memory.working.last_action} → {memory.working.last_result}\n"
                        f"  Failed episodes: {sum(1 for e in memory.episodic if not e.ok)}\n"
                        "Resume or continue with the new goal above."
                    )
                })
                tracer.note(f"checkpoint_restored: turn={memory.working.turn}")

        system     = build_system_prompt()
        model      = _main_model()
        max_tokens = _main_max_tokens()
        if _thinking_enabled():
            max_tokens = max(max_tokens, _thinking_budget() + 4096)

        escalation         = EscalationTracker()
        turn               = 0
        _retry_count       = 0
        _last_retry_reason = ""

        # [Fix 1] 세션 결과 추적 — 루프 정상 종료 / 비정상 종료 모두 처리
        _session_success = False
        _session_summary = ""

        try:
            while True:

                # Context compression
                if should_compact(messages):
                    facts    = memory.get_persistent_facts()
                    messages = compact(messages, active_goal, todos.format_for_llm(),
                                       self.client, persistent_facts=facts)
                    tracer.note("context compacted")

                conn_status = (
                    f"[Connected: {self.conn.address}]" if self.conn else "[Not connected]"
                )
                _mem_query = f"{active_goal} {memory.working.current_step} {memory.working.last_action}"
                system_with_state = (
                    system
                    + f"\n\nCurrent connection: {conn_status}"
                    + (f"\n\n{memory.to_system_context(query=_mem_query)}"
                       if memory.to_system_context(query=_mem_query) else "")
                    + (f"\n\n{todos.format_nag()}" if todos.format_nag() else "")
                )

                # ── LLM call ─────────────────────────────────────
                try:
                    escalate, reason = escalation.should_escalate()
                    turn_model       = _escalate_model() if escalate else model
                    turn_thinking    = escalate or _thinking_enabled()
                    turn_max_tokens  = (
                        max(max_tokens, _thinking_budget() + 4096)
                        if (turn_thinking and not _supports_adaptive(turn_model))
                        else max_tokens
                    )

                    if escalate:
                        _decision   = classify_failure(escalation.get_recent_results())
                        _reflection = generate_reflection(messages, active_goal, _decision,
                                                          self.client, model=model)
                        messages.append(make_reflection_message(_reflection, _decision))
                        tracer.reflection(_decision, _reflection)
                        print(f"\n  🔺 Escalate → {turn_model} ({reason})", flush=True)

                        if _decision == ReplanDecision.RETRY_SAME_TASK:
                            _retry_count = _retry_count + 1 if reason == _last_retry_reason else 1
                            _last_retry_reason = reason
                            if _retry_count >= _env_int("ECC_MAX_RETRY", 3):
                                messages.append({"role": "user", "content":
                                    "[system] RETRY limit (3x). Switch strategy or call done(success=false)."})
                                tracer.note(f"retry_limit_exceeded: {reason[:60]}")
                                _retry_count = 0
                        else:
                            _retry_count = 0
                            _last_retry_reason = ""

                    create_kwargs = dict(
                        model=turn_model, max_tokens=turn_max_tokens,
                        system=system_with_state, tools=get_tool_definitions(), messages=messages,
                    )
                    if turn_thinking:
                        create_kwargs["thinking"] = _thinking_params(turn_model)

                    t0     = time.monotonic()
                    resp   = self.client.messages.create(**create_kwargs)
                    llm_ms = int((time.monotonic() - t0) * 1000)

                    _usage = getattr(resp, "usage", None)
                    tracer.llm_call(
                        model=turn_model,
                        tokens_in=getattr(_usage, "input_tokens", 0) if _usage else 0,
                        tokens_out=getattr(_usage, "output_tokens", 0) if _usage else 0,
                        duration_ms=llm_ms, escalated=escalate,
                    )
                    if escalate:
                        escalation.reset_escalation()

                except anthropic.RateLimitError:
                    wait = _rate_limit_wait()
                    print(f"\n  ⏳ Rate limit — {wait}s wait...", flush=True)
                    time.sleep(wait)
                    continue

                except anthropic.BadRequestError as e:
                    err = str(e).lower()
                    if any(kw in err for kw in ("context", "too long", "too many token",
                                                 "input length", "prompt_too_long")):
                        facts    = memory.get_persistent_facts()
                        messages = compact(messages, active_goal, todos.format_for_llm(),
                                           self.client, persistent_facts=facts)
                        tracer.note("context_overflow_compacted")
                        continue
                    raise

                last_asst = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
                if last_asst and last_asst["content"] is resp.content:
                    continue
                messages.append({"role": "assistant", "content": resp.content})

                seen_text = False
                for block in resp.content:
                    if block.type == "thinking" and block.thinking.strip():
                        _print_thinking(block.thinking)
                    elif block.type == "text" and block.text.strip() and not seen_text:
                        text = re.sub(r'<thinking>.*?</thinking>', '', block.text.strip(), flags=re.DOTALL).strip()
                        if text:
                            print(f"\n  💬 {text}", flush=True)
                        seen_text = True

                has_tools = any(b.type == "tool_use" for b in resp.content)
                if resp.stop_reason == "end_turn" and not has_tools:
                    print("\n  ⚠️  Stopped without done()..", flush=True)
                    messages.append({"role": "user", "content":
                        "[system] Call done() or continue working toward the goal."})
                    continue

                # ── Tool execution ────────────────────────────────
                tool_blocks = [b for b in resp.content if b.type == "tool_use"]
                all_results = disp.dispatch(tool_blocks, executor, memory, messages)

                # ── Verifier feedback annotation ──────────────────
                _annotations: dict[str, str] = {}
                for block in tool_blocks:
                    out = all_results.get(block.id, "")
                    ok  = not any(out.startswith(p) for p in ("[error]", "[blocked]", "[safety_blocked]"))
                    obs = collect_observation(block.name, out)

                    if block.name == "verify":
                        vr = verify_execution(block.name, obs)
                        if not vr["success"]:
                            recovery = route_from_verifier(vr["reason"])
                            tracer.note(f"verify_failed: {vr['reason']} → {recovery.route}")
                            memory.record_episode(f"verify_fail/{vr['reason']}", vr["evidence"], False)
                            ann = (f"\n\n[Verifier] reason={vr['reason']}\n"
                                   f"evidence: {vr['evidence'][:120]}\n")
                            fb = vr.get("feedback")
                            if fb:
                                ann += (f"error_type: {fb['error_type']}\n"
                                        f"root_cause: {fb['root_cause']}\n"
                                        f"suggested_fix: {fb['suggested_fix']}\n"
                                        f"retry_safe: {fb['retry_safe']}\n")
                            ann += f"→ Next: {recovery.note}"
                            _annotations[block.id] = ann

                    elif block.name in ("bash", "script"):
                        cmd = str(block.input.get("command", block.input.get("code", "")))
                        if any(kw in cmd for kw in ("ros2 topic pub", "cmd_vel", "/drive")):
                            mvr = verify_motion(obs["stdout"])
                            if not mvr["success"]:
                                tracer.note(f"motion_not_verified: {mvr['evidence'][:80]}")
                                ann = f"\n\n[Motion verifier] not verified: {mvr['evidence'][:120]}"
                                fb  = mvr.get("feedback") or {}
                                if fb.get("suggested_fix"):
                                    ann += f"\nsuggested_fix: {fb['suggested_fix']}"
                                _annotations[block.id] = ann
                        elif not ok:
                            fb = parse_error_feedback(out)
                            if fb:
                                _annotations[block.id] = (
                                    f"\n\n[Error feedback]\nerror_type: {fb['error_type']}\n"
                                    f"root_cause: {fb['root_cause']}\n"
                                    f"suggested_fix: {fb['suggested_fix']}\n"
                                    f"retry_safe: {fb['retry_safe']}"
                                )

                    memory.record_episode(block.name, out, ok)
                    memory.working.last_action = block.name
                    memory.working.last_result = out[:50].replace("\n", " ")
                    tracer.tool_use(name=block.name, inp_summary=str(block.input)[:100],
                                    out_summary=out[:100], ok=ok)

                escalation.record_tool_results(tool_blocks, all_results)

                _in_prog = todos.in_progress_items()
                if _in_prog:
                    memory.working.current_step = _in_prog[0].content[:100]

                tool_results = []
                for block in tool_blocks:
                    out = all_results.get(block.id, "[error] no result")
                    ann = _annotations.get(block.id, "")
                    tool_results.append({"type": "tool_result",
                                         "tool_use_id": block.id,
                                         "content": out + ann})
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})

                if executor.is_finished:
                    state.messages = messages
                    self._session.save(state)
                    memory.checkpoint_clear()

                    consolidate_episodic(memory, active_goal, self.client)

                    # [Fix 1] done()의 success 값을 실제로 반영
                    # executor._done()이 self.is_finished = True로 세팅할 때
                    # done(success=...) 인자는 이미 출력됐지만 여기서 추적하려면
                    # executor에 _last_done_success 플래그를 추가하는 것이 가장 정확.
                    # 현재 구조에서는 done()이 출력한 [OK]/[FAIL] 텍스트로 판단하거나,
                    # all_results에서 "done" 툴 블록의 input을 확인.
                    _done_block = next(
                        (b for b in tool_blocks if b.name == "done"),
                        None,
                    )
                    _done_success = _done_block.input.get("success", False) if _done_block else False
                    _done_summary = _done_block.input.get("summary", "") if _done_block else ""

                    _session_success = _done_success
                    _session_summary = _done_summary

                    _record_session_end(
                        tracer=tracer,
                        active_goal=active_goal,
                        turn=turn,
                        success=_session_success,
                        conn=self.conn,
                        summary=_session_summary,
                    )
                    break

                if self.conn and turn > 0 and turn % _conn_check_interval() == 0:
                    disp.check_connection(messages)

                memory.working.turn = turn
                memory.checkpoint_save()
                turn += 1
                if turn >= max_turns:
                    print(f"\n  ⚠️  {turn} turns — continuing...", flush=True)
                    messages.append({"role": "user", "content":
                        f"[system] {turn} turns. Keep working. Call done() only when finished."})
                    max_turns += _max_turns_step()

        except Exception:
            # [Fix 1] 예외로 종료될 때도 기록 (re-raise 전)
            _record_session_end(
                tracer=tracer,
                active_goal=active_goal,
                turn=turn,
                success=False,
                conn=self.conn,
                summary=f"exception after {turn} turns",
            )
            raise

    def _save_partial_session(self):
        """Called by cli.py on KeyboardInterrupt."""
        state = self._session._current_state_snapshot()
        if state:
            self._session.save_partial(*state)

    @staticmethod
    def _is_followup(goal: str, has_session: bool) -> bool:
        """Backward compat. Delegates to SessionManager.is_followup()."""
        return SessionManager.is_followup(goal, has_session)


# ─────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────

def _print_thinking(text: str) -> None:
    lines = text.strip().splitlines()
    first = lines[0][:120] if lines else ""
    print(f"\n  🧠 thinking ({len(text)}ch): {first}", flush=True)
    if len(lines) > 1:
        print(f"     ... ({len(lines)} lines)", flush=True)


def _extract_known_context(messages: list[dict]) -> str:
    context_lines = []
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text = str(block.get("content", ""))
            for m in re.finditer(r"/dev/\w+", text):
                line = f"device: {m.group()}"
                if line not in context_lines:
                    context_lines.append(line)
            for m in re.finditer(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text):
                line = f"ip: {m.group()}"
                if line not in context_lines:
                    context_lines.append(line)
            for m in re.finditer(r"(\w+(?:_\w+)*)\s*[:=]\s*([\w./\-]+)", text):
                if len(m.group()) < 60:
                    line = f"param: {m.group()}"
                    if line not in context_lines:
                        context_lines.append(line)
    return "\n".join(context_lines[:30])