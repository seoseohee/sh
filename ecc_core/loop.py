"""
ecc_core/loop.py — AgentLoop (thin orchestrator)

Changelog:
  v5 — [Fix 1] record_goal 누락 수정 (모든 종료 경로 기록)
  v6 — [Fix] except BaseException, set_running() 추가
  v7 — [Improve A] 툴 결과를 messages에 넣기 전 summarize_tool_output() 적용.
         원본 전체 텍스트 대신 툴별 요약본이 컨텍스트에 누적됨.
       [Improve E] 세션 중 에피소딕→시맨틱 통합.
         실패 에피소드 5개 누적마다 consolidate_episodic() 호출해
         같은 세션 내 반복 실수를 즉시 failed 네임스페이스에 저장.
         ECC_MID_SESSION_CONSOLIDATE_EVERY (기본 5)로 조정.
       [Improve G] should_ask_user() 메타인지 라우팅.
         에스컬레이션 임계보다 높은 수준의 반복 실패 감지 시
         LLM에 ask_user() 도구 호출을 명시적으로 지시하는 시스템 메시지 주입.
       [Improve C] classify_failure에 client 인자 전달 (LLM fallback 활성화).
"""

import os
import re
import time
import anthropic
from concurrent.futures import ThreadPoolExecutor, as_completed

from .connection    import BoardConnection, BoardDiscovery
from .todo          import TodoManager
from .executor      import ToolExecutor
from .compactor     import should_compact, compact, summarize_tool_output
from .prompt        import build_system_prompt
from .tool_schemas  import get_tool_definitions
from .memory        import ECCMemory
from .tracer        import Tracer
from .reflection    import (classify_failure, generate_reflection,
                             make_reflection_message, route_from_verifier, ReplanDecision)
from .observation   import collect_observation
from .verifier      import verify_execution, verify_motion, parse_error_feedback
from .escalation    import EscalationTracker
from .session       import SessionManager
from .dispatcher    import ToolDispatcher, SubagentRole, run_subagent  # noqa: F401
from .consolidation import consolidate_episodic
from .goal_history  import record_goal


# ─────────────────────────────────────────────────────────────
# Env helpers
# ─────────────────────────────────────────────────────────────

def _env_int(key: str, default: int) -> int:
    try: return int(os.environ.get(key, default))
    except (ValueError, TypeError): return default

def _main_model() -> str:
    return os.environ.get("ECC_MODEL", "claude-sonnet-4-6")

def _escalate_model() -> str:
    env = os.environ.get("ECC_ESCALATE_MODEL")
    if env: return env
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
    if env: return any(m.strip() in model for m in env.split(","))
    m = re.search(r"(\d+)[\.-](\d+)", model)
    if m: return (int(m.group(1)), int(m.group(2))) >= (4, 6)
    return False

def _thinking_params(model: str) -> dict:
    return {"type": "adaptive"} if _supports_adaptive(model) else \
           {"type": "enabled", "budget_tokens": _thinking_budget()}

def _rate_limit_wait() -> int:
    return _env_int("ECC_RATE_LIMIT_WAIT", 60)

def _conn_check_interval() -> int:
    return _env_int("ECC_CONN_CHECK_INTERVAL", 10)

def _max_turns_step() -> int:
    return _env_int("ECC_MAX_TURNS_STEP", 50)

def _mid_session_consolidate_every() -> int:
    """[Improve E] 실패 에피소드 몇 개마다 중간 통합할지. ECC_MID_SESSION_CONSOLIDATE_EVERY."""
    return _env_int("ECC_MID_SESSION_CONSOLIDATE_EVERY", 5)


# ─────────────────────────────────────────────────────────────
# 세션 종료 헬퍼
# ─────────────────────────────────────────────────────────────

def _record_session_end(tracer, active_goal, turn, success, conn, summary="") -> None:
    tok_in, tok_out = tracer.get_token_totals()
    tracer.session_end(
        success=success,
        summary=summary or (f"done() after {turn} turns" if success else f"failed after {turn} turns"),
    )
    record_goal(
        goal=active_goal, success=success, turns=turn,
        conn_address=conn.address if conn else "",
        tokens_in=tok_in, tokens_out=tok_out,
    )


# ─────────────────────────────────────────────────────────────
# AgentLoop
# ─────────────────────────────────────────────────────────────

class AgentLoop:

    def __init__(self, verbose: bool = False):
        self.verbose  = verbose
        self.client   = anthropic.Anthropic()
        self.conn: BoardConnection | None = None
        self._session = SessionManager()

    # ── cli.py 하위 호환 프로퍼티 ──────────────────────────────

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

    # ── 메인 루프 ──────────────────────────────────────────────

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

        if not is_followup and memory.checkpoint_exists():
            restored = memory.checkpoint_load()
            if restored and memory.working.goal:
                print(f"\n  ♻️  Checkpoint restored — turn={memory.working.turn}"
                      f", step='{memory.working.current_step[:40]}'", flush=True)
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
        _session_success   = False
        _session_summary   = ""

        # [Improve E] 중간 통합 추적 카운터
        _last_consolidate_fail_count = 0

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

                # ── LLM 호출 ─────────────────────────────────────
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
                        # [Improve C] classify_failure에 client 전달 → LLM fallback 활성화
                        _decision   = classify_failure(escalation.get_recent_results(), self.client)
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

                    # [Improve G] 메타인지 판단 — ask_user 라우팅
                    ask_user_needed, ask_user_reason = escalation.should_ask_user()
                    if ask_user_needed:
                        print(f"\n  🤔 Meta-cognition: ask_user recommended — {ask_user_reason}", flush=True)
                        tracer.note(f"meta_ask_user: {ask_user_reason[:80]}")
                        messages.append({
                            "role": "user",
                            "content": (
                                f"[system] Meta-cognitive signal: {ask_user_reason}\n"
                                "You should call ask_user() to report the situation and ask for direction "
                                "rather than continuing the current approach. "
                                "Explain clearly what you have tried and what the obstacle is."
                            )
                        })
                        # 중복 주입 방지: sig_counter 리셋 (ask_user 호출 기회 1회 부여)
                        escalation._sig_counter.clear()

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

                # ── 툴 실행 ───────────────────────────────────────
                tool_blocks = [b for b in resp.content if b.type == "tool_use"]
                all_results = disp.dispatch(tool_blocks, executor, memory, messages)

                # ── Verifier 피드백 어노테이션 ────────────────────
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

                # [Improve A] 툴 결과를 messages에 넣기 전 요약 적용
                tool_results = []
                for block in tool_blocks:
                    out = all_results.get(block.id, "[error] no result")
                    ann = _annotations.get(block.id, "")
                    # 요약 적용 (어노테이션이 있는 경우 원본 + 어노테이션, 없으면 요약본)
                    summarized = summarize_tool_output(block.name, out)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": summarized + ann,
                    })
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})

                # [Improve E] 세션 중 에피소딕 → 시맨틱 통합
                current_fail_count = sum(1 for e in memory.episodic if not e.ok)
                consolidate_every  = _mid_session_consolidate_every()
                if (consolidate_every > 0
                        and current_fail_count >= consolidate_every
                        and current_fail_count - _last_consolidate_fail_count >= consolidate_every):
                    _result = consolidate_episodic(memory, active_goal, self.client, min_failures=1)
                    if _result["failed"] + _result["constraints"] > 0:
                        tracer.note(
                            f"mid_session_consolidate: "
                            f"failed={_result['failed']} constraints={_result['constraints']}"
                        )
                    _last_consolidate_fail_count = current_fail_count

                if executor.is_finished:
                    state.messages = messages
                    self._session.save(state)
                    memory.checkpoint_clear()

                    consolidate_episodic(memory, active_goal, self.client)

                    _done_block   = next((b for b in tool_blocks if b.name == "done"), None)
                    _done_success = _done_block.input.get("success", False) if _done_block else False
                    _done_summary = _done_block.input.get("summary", "") if _done_block else ""

                    _session_success = _done_success
                    _session_summary = _done_summary

                    _record_session_end(
                        tracer=tracer, active_goal=active_goal, turn=turn,
                        success=_session_success, conn=self.conn, summary=_session_summary,
                    )
                    break

                if self.conn and turn > 0 and turn % _conn_check_interval() == 0:
                    disp.check_connection(messages)

                memory.working.turn = turn
                memory.checkpoint_save()
                self._session.set_running(messages, active_goal, todos, executor, memory)
                turn += 1
                if turn >= max_turns:
                    print(f"\n  ⚠️  {turn} turns — continuing...", flush=True)
                    messages.append({"role": "user", "content":
                        f"[system] {turn} turns. Keep working. Call done() only when finished."})
                    max_turns += _max_turns_step()

        except BaseException:
            _record_session_end(
                tracer=tracer, active_goal=active_goal, turn=turn,
                success=False, conn=self.conn, summary=f"exception after {turn} turns",
            )
            raise

    def _save_partial_session(self):
        state = self._session._current_state_snapshot()
        if state:
            self._session.save_partial(*state)

    @staticmethod
    def _is_followup(goal: str, has_session: bool) -> bool:
        return SessionManager.is_followup(goal, has_session)


# ─────────────────────────────────────────────────────────────
# 출력 헬퍼
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