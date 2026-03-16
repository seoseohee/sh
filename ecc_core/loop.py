"""
ecc_core/loop.py

ECC — Embedded Claude Code 에이전트 루프.

CC 아키텍처를 임베디드 환경으로 확장:

  CC 원본 구조                   ECC 대응
  ──────────────────────────── ──────────────────────────────────────
  Bash(local cmd)              bash(SSH remote cmd)
  Write(local file)            script() / write() on board
  TaskOutput(async)            bash(background=True) + bash_wait()
  Read(local file)             read(remote path)
  Tool dispatcher              ToolExecutor + BoardConnection
  Context compaction           compactor.py (동일 방식)
  Parallel tool execution      ThreadPoolExecutor (동일 방식)
  Subagent (Task tool)         run_subagent() with report()

추가된 ECC 전용 레이어:
  - BoardConnection: SSH 세션 추상화
  - EscalationTracker: 반복 실패 감지 → opus+thinking 자동 전환
  - ECCMemory: 3-tier 메모리 (Working / Episodic / Semantic)
  - Tracer: JSONL 관찰 가능성 레이어
  - Reflection: Reflexion 패턴 기반 3-way replan 분기

환경변수:
  ECC_MODEL            메인 에이전트 모델 (기본: claude-sonnet-4-6)
  ECC_ESCALATE_MODEL   escalation 시 모델 (기본: sonnet→opus 자동 치환)
  ECC_ADAPTIVE_MODELS  adaptive thinking 지원 모델, 쉼표 구분
  ECC_MAX_TOKENS       메인 에이전트 max_tokens (기본: 8096)
  ECC_THINKING         1이면 thinking 항상 활성화
  ECC_THINKING_BUDGET  thinking budget_tokens (기본: 8000)
  ECC_COMPACT_MODEL    컨텍스트 압축용 모델 (기본: ECC_MODEL)
  ECC_SUBAGENT_TURNS   subagent 최대 루프 수 (기본: 40)
  ECC_TRACE            0이면 JSONL 트레이싱 비활성화 (기본: 1)
"""

import os
import re
import time
import anthropic
from concurrent.futures import ThreadPoolExecutor, as_completed

from .connection import BoardConnection, BoardDiscovery
from .todo import TodoManager
from .executor import ToolExecutor
from .compactor import should_compact, compact
from .prompt import build_system_prompt
from .tools import TOOL_DEFINITIONS, get_tool_definitions
from .memory import ECCMemory
from .tracer import Tracer
from .reflection import classify_failure, generate_reflection, make_reflection_message, route_from_verifier
from .observation import collect_observation
from .verifier import verify_execution, verify_motion


# ─────────────────────────────────────────────────────────────
# 환경변수 헬퍼
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
    if "sonnet" in main:
        return main.replace("sonnet", "opus")
    return main

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
        major, minor = int(m.group(1)), int(m.group(2))
        return (major, minor) >= (4, 6)
    return False

def _thinking_params(model: str) -> dict:
    if _supports_adaptive(model):
        return {"type": "adaptive"}
    return {"type": "enabled", "budget_tokens": _thinking_budget()}


# ─────────────────────────────────────────────────────────────
# Subagent — CC의 Task tool에 해당
# ─────────────────────────────────────────────────────────────

SUBAGENT_TOOLS = [
    t for t in get_tool_definitions()
    if t["name"] not in ("subagent", "done")
] + [
    {
        "name": "report",
        "description": "Exploration complete. Return findings to the main agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "string",
                    "description": (
                        "Complete findings summary. "
                        "Include specific values: device paths, IP addresses, "
                        "parameter names/values, topic names, service names, versions. "
                        "The main agent will act on this without re-investigating."
                    )
                }
            },
            "required": ["findings"]
        }
    }
]


def run_subagent(
    goal: str,
    context: str,
    conn: BoardConnection,
    client: anthropic.Anthropic,
    verbose: bool = False,
) -> str:
    system = (
        "You are a subagent for ECC. Perform the given task and call report().\n"
        "Be thorough. Batch independent commands. Do NOT spawn subagents.\n"
        "Include specific values in your report: paths, addresses, parameters, versions.\n"
        f"SSH: {conn.user}@{conn.host}:{conn.port}\n"
        + (f"\nAlready known:\n{context}" if context else "")
    )

    todos = TodoManager()
    executor = ToolExecutor(conn, todos, verbose)
    messages: list[dict] = [{"role": "user", "content": goal}]
    max_turns = _env_int("ECC_SUBAGENT_TURNS", 40)
    turn = 0

    while True:
        resp = client.messages.create(
            model=_main_model(),
            max_tokens=4096,
            system=system,
            tools=SUBAGENT_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_results = []
        findings = ""
        finished = False

        SUBAGENT_PARALLEL = {"bash", "bash_wait", "script", "read", "write",
                             "glob", "grep", "probe", "verify", "todo"}

        serial_blocks   = [b for b in resp.content
                           if b.type == "tool_use" and b.name not in SUBAGENT_PARALLEL]
        parallel_blocks = [b for b in resp.content
                           if b.type == "tool_use" and b.name in SUBAGENT_PARALLEL]

        all_results: dict[str, str] = {}

        for block in serial_blocks:
            if block.name == "report":
                findings = block.input.get("findings", "")
                all_results[block.id] = "reported"
                finished = True
            else:
                all_results[block.id] = executor.execute(block.name, block.input)

        if parallel_blocks and not finished:
            with ThreadPoolExecutor(max_workers=min(len(parallel_blocks), 8)) as pool:
                futures = {
                    pool.submit(executor.execute, b.name, b.input): b.id
                    for b in parallel_blocks
                }
                for future in as_completed(futures):
                    bid = futures[future]
                    try:
                        all_results[bid] = future.result()
                    except Exception as e:
                        all_results[bid] = f"[error] {e}"

        for block in resp.content:
            if block.type != "tool_use":
                continue
            out = all_results.get(block.id, "[error] no result")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": out,
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if finished:
            return findings

        if resp.stop_reason == "end_turn" and not any(
            b.type == "tool_use" for b in resp.content
        ):
            messages.append({
                "role": "user",
                "content": (
                    "[system] You stopped without calling report(). "
                    "Complete the task and call report() with your findings."
                )
            })
            continue

        turn += 1
        if turn >= max_turns:
            messages.append({
                "role": "user",
                "content": (
                    f"[system] {turn} turns elapsed. "
                    "Wrap up and call report() with what you have found so far."
                )
            })
            max_turns += 20

    return "(subagent: no report)"


# ─────────────────────────────────────────────────────────────
# AgentLoop
# ─────────────────────────────────────────────────────────────

class AgentLoop:

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.client = anthropic.Anthropic()
        self.conn: BoardConnection | None = None
        self._session_messages: list[dict] = []
        self._session_goal: str = ""
        self._session_todos: "TodoManager | None" = None
        self._session_executor: "ToolExecutor | None" = None
        self._session_memory: "ECCMemory | None" = None

    @staticmethod
    def _is_followup(goal: str, has_session: bool) -> bool:
        stripped = goal.strip()
        if not has_session:
            return False
        if stripped.startswith("/"):
            return False
        return len(stripped.split()) <= 6

    def run(self, goal: str, max_turns: int = 100):
        print(f"\n{'═'*60}")
        print(f"  🎯 {goal[:80]}")
        print(f"{'═'*60}")

        if self.conn and not self.conn.is_alive():
            print("  ⚠️  이전 연결이 끊어짐. 에이전트가 재연결합니다.")
            self.conn = None

        is_followup = self._is_followup(goal, bool(self._session_messages))
        active_goal = self._session_goal if is_followup else goal

        # ── Memory 먼저 초기화 — executor에 전달하기 위해 앞으로 이동 ──
        memory = self._session_memory if (is_followup and self._session_memory) else \
                 ECCMemory(conn_address=self.conn.address if self.conn else "")
        memory.working.goal = active_goal
        tracer = Tracer(goal=active_goal[:80])
        # ─────────────────────────────────────────────────────────

        if is_followup:
            todos = self._session_todos or TodoManager()
            executor = self._session_executor or ToolExecutor(
                self.conn, todos, memory=memory, verbose=self.verbose
            )
            executor.conn = self.conn
            executor.memory = memory   # 재사용 executor도 최신 memory로 갱신
            executor.is_finished = False
            messages = self._session_messages + [
                {"role": "user", "content": f"[User follow-up] {goal}"}
            ]
            print(f"  🔁 이전 세션 이어받기 ({len(self._session_messages)}개 메시지)", flush=True)
        else:
            todos = TodoManager()
            executor = ToolExecutor(self.conn, todos, memory=memory, verbose=self.verbose)
            messages: list[dict] = [{"role": "user", "content": goal}]

        self._current_goal = active_goal
        self._current_todos = todos
        self._current_executor = executor
        self._current_messages: list[dict] = messages
        self._current_memory = memory

        system = build_system_prompt()

        model = _main_model()
        max_tokens = _main_max_tokens()
        if _thinking_enabled():
            max_tokens = max(max_tokens, _thinking_budget() + 4096)

        escalation = EscalationTracker()
        turn = 0
        _retry_count = 0          # ④ RETRY_SAME_TASK 연속 횟수 추적
        _last_retry_reason = ""   # 동일 원인 무한 retry 방지

        while True:

            # 컨텍스트 압축 — persistent_facts를 보존해서 재발견 방지
            if should_compact(messages):
                facts = memory.get_persistent_facts()
                messages = compact(
                    messages, active_goal, todos.format_for_llm(),
                    self.client, persistent_facts=facts
                )
                tracer.note("context compacted")

            # 매 turn: 연결 상태 + memory context + todo nag 주입
            conn_status = (
                f"[Connected: {self.conn.address}]"
                if self.conn else
                "[Not connected — call ssh_connect first]"
            )
            mem_ctx = memory.to_system_context()
            nag = todos.format_nag()
            system_with_state = (
                system
                + f"\n\nCurrent connection: {conn_status}"
                + (f"\n\n{mem_ctx}" if mem_ctx else "")
                + (f"\n\n{nag}" if nag else "")
            )

            try:
                escalate, reason = escalation.should_escalate()
                turn_model    = _escalate_model() if escalate else model
                turn_thinking = escalate or _thinking_enabled()

                if turn_thinking and not _supports_adaptive(turn_model):
                    turn_max_tokens = max(max_tokens, _thinking_budget() + 4096)
                else:
                    turn_max_tokens = max_tokens

                # ── Reflexion: escalation 트리거 시 언어 반성 생성 ──
                if escalate:
                    _decision = classify_failure(escalation.get_recent_results())
                    _reflection = generate_reflection(
                        messages, active_goal, _decision, self.client, model=model
                    )
                    _ref_msg = make_reflection_message(_reflection, _decision)
                    messages.append(_ref_msg)
                    tracer.reflection(_decision, _reflection)
                    print(
                        f"\n  🔺 Escalate → {turn_model} + thinking ({reason})",
                        flush=True
                    )
                    print(
                        f"  🪞 [{_decision}] {_reflection[:100]}",
                        flush=True
                    )

                    # ④ Recovery Router — RETRY_SAME_TASK: 기계적 재시도 제한
                    from .reflection import ReplanDecision
                    if _decision == ReplanDecision.RETRY_SAME_TASK:
                        if reason == _last_retry_reason:
                            _retry_count += 1
                        else:
                            _retry_count = 1
                            _last_retry_reason = reason

                        if _retry_count >= 3:
                            # 동일 원인으로 3회 RETRY → REVISE로 강제 상향
                            _override = (
                                "[system] RETRY limit reached (3x same failure). "
                                "Switch strategy — do NOT retry the same approach. "
                                "Revise your method or call done(success=false)."
                            )
                            messages.append({"role": "user", "content": _override})
                            tracer.note(f"retry_limit_exceeded: {reason[:60]}")
                            _retry_count = 0
                    else:
                        # REVISE / REPLAN → retry 카운터 리셋
                        _retry_count = 0
                        _last_retry_reason = ""
                # ────────────────────────────────────────────────────

                create_kwargs = dict(
                    model=turn_model,
                    max_tokens=turn_max_tokens,
                    system=system_with_state,
                    tools=get_tool_definitions(),
                    messages=messages,
                )
                if turn_thinking:
                    create_kwargs["thinking"] = _thinking_params(turn_model)

                # LLM 호출 + 타이밍 기록
                t0_llm = time.monotonic()
                resp = self.client.messages.create(**create_kwargs)
                llm_ms = int((time.monotonic() - t0_llm) * 1000)

                _usage = getattr(resp, "usage", None)
                tracer.llm_call(
                    model=turn_model,
                    tokens_in=getattr(_usage, "input_tokens", 0) if _usage else 0,
                    tokens_out=getattr(_usage, "output_tokens", 0) if _usage else 0,
                    duration_ms=llm_ms,
                    escalated=escalate,
                )

                if escalate:
                    escalation.reset_escalation()

            except anthropic.RateLimitError:
                wait = 60
                print(f"\n  ⏳ Rate limit (429) — {wait}초 대기 후 재시도...", flush=True)
                tracer.note(f"rate_limit_wait_{wait}s")
                time.sleep(wait)
                continue

            except anthropic.BadRequestError as e:
                err_msg = str(e).lower()
                is_context_error = any(kw in err_msg for kw in (
                    "context", "too long", "too many token", "input length",
                    "prompt_too_long", "prompt is too long",
                ))
                if is_context_error:
                    print(f"\n  ⚠️  컨텍스트 초과 — 압축 후 재시도", flush=True)
                    facts = memory.get_persistent_facts()
                    messages = compact(
                        messages, active_goal, todos.format_for_llm(),
                        self.client, persistent_facts=facts
                    )
                    tracer.note("context_overflow_compacted")
                    continue
                raise

            # 중복 append 방지
            last_assistant = next(
                (m for m in reversed(messages) if m["role"] == "assistant"), None
            )
            if last_assistant and last_assistant["content"] is resp.content:
                continue
            messages.append({"role": "assistant", "content": resp.content})

            # 출력: thinking → text
            seen_text = False
            for block in resp.content:
                if block.type == "thinking" and block.thinking.strip():
                    _print_thinking(block.thinking)
                elif block.type == "text" and block.text.strip():
                    if not seen_text:
                        text = block.text.strip()
                        text = re.sub(r'<thinking>.*?</thinking>', '', text,
                                      flags=re.DOTALL).strip()
                        if text:
                            print(f"\n  💬 {text}", flush=True)
                        seen_text = True

            # end_turn without done() → 다시 밀어줌
            has_tools = any(b.type == "tool_use" for b in resp.content)
            if resp.stop_reason == "end_turn" and not has_tools:
                print("\n  ⚠️  done() 없이 멈춤. 계속 진행 요청...", flush=True)
                messages.append({
                    "role": "user",
                    "content": (
                        "[system] You stopped without calling done(). "
                        "The goal is not complete until you explicitly call done(). "
                        "Continue working toward the goal, or call done(success=false) "
                        "if it is proven impossible."
                    )
                })
                continue

            # ── Tool 실행 ──────────────────────────────────────────
            tool_blocks = [b for b in resp.content if b.type == "tool_use"]

            PARALLEL_TOOLS = {
                "bash", "bash_wait", "script",
                "read", "write", "glob", "grep",
                "probe", "verify", "todo",
                "serial_open", "serial_send", "serial_close",
                "remember",   # 메모리 쓰기 — 상태 의존성 없으므로 병렬 허용
            }

            serial_blocks   = [b for b in tool_blocks
                               if b.name not in PARALLEL_TOOLS or self.conn is None]
            parallel_blocks = [b for b in tool_blocks
                               if b.name in PARALLEL_TOOLS and self.conn is not None]

            serial_results: dict[str, str] = {}
            for block in serial_blocks:
                if block.name == "ssh_connect":
                    out = self._handle_ssh_connect(block.input)
                    executor.conn = self.conn

                    # ssh_connect 성공 시 memory에 보드 연결 정보 업데이트
                    if self.conn:
                        memory.update_connection(self.conn.address)

                elif self.conn is None and block.name not in {
                    "bash", "bash_wait", "read", "write", "glob", "grep",
                    "todo", "done", "ask_user",
                }:
                    out = (
                        "[no connection] SSH connection required before using this tool.\n"
                        "Call ssh_connect first. If you don't know the host, use host='scan'."
                    )

                elif block.name == "subagent":
                    known = _extract_known_context(messages)
                    out = run_subagent(
                        goal=block.input.get("goal", ""),
                        context=block.input.get("context", known),
                        conn=self.conn,
                        client=self.client,
                        verbose=self.verbose,
                    )

                elif block.name == "ask_user":
                    out = executor.execute("ask_user", block.input)

                else:
                    # ③ affordance check — memory.can_execute()로 실행 가능성 사전 검증
                    task_hint = f"{block.name} {str(block.input)[:80]}"
                    feasible, reason = memory.can_execute(task_hint)
                    if not feasible:
                        out = (
                            f"[can_execute blocked] {reason}\n"
                            "Run probe() first to confirm hardware availability."
                        )
                    else:
                        out = executor.execute(block.name, block.input)

                serial_results[block.id] = out

            parallel_results: dict[str, str] = {}
            if parallel_blocks:
                with ThreadPoolExecutor(max_workers=min(len(parallel_blocks), 8)) as pool:
                    futures = {
                        pool.submit(executor.execute, b.name, b.input): b.id
                        for b in parallel_blocks
                    }
                    for future in as_completed(futures):
                        bid = futures[future]
                        try:
                            parallel_results[bid] = future.result()
                        except Exception as e:
                            parallel_results[bid] = f"[error] {e}"

            all_results = {**serial_results, **parallel_results}

            # tool_results 조립
            tool_results = []
            for block in tool_blocks:
                out = all_results.get(block.id, "[error] no result")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out,
                })

            # escalation + memory episodic + tracer 기록
            escalation.record_tool_results(tool_blocks, all_results)
            _deferred_verify_messages: list[dict] = []  # verifier 결과 inject용

            for block in tool_blocks:
                out = all_results.get(block.id, "")
                ok = not (out.startswith("[error]") or out.startswith("[blocked]"))

                # ── Observation Collector: tool_result → 공통 스키마 ──
                obs = collect_observation(block.name, out)

                # ── Execution Verifier: verify / motion 도구 특화 판정 ──
                # 우선순위: verifier specific reason > keyword classifier (fallback)
                # Sumers et al. (TPAMI 2024): 직접 관찰 기반 판정이 heuristic보다 우선
                if block.name == "verify":
                    vr = verify_execution(block.input.get("target", ""), obs)
                    if not vr["success"]:
                        recovery = route_from_verifier(vr["reason"])
                        tracer.note(
                            f"verify_failed: {vr['reason']} → {recovery.route}"
                        )
                        memory.record_episode(
                            f"verify_fail/{vr['reason']}", vr["evidence"], False
                        )
                        # ← Fix: verifier 판정을 LLM에 inject (이전엔 tracer.note만)
                        # escalation이 같은 실패를 REVISE로 잡더라도
                        # verifier specific reason이 더 정확하므로 별도 메시지로 보강
                        _vr_inject = make_reflection_message(
                            f"Verification failed ({vr['reason']}).\n"
                            f"Evidence: {vr['evidence'][:150]}",
                            recovery.route,
                        )
                        _deferred_verify_messages.append(_vr_inject)

                elif block.name in ("bash", "script"):
                    cmd = str(block.input.get("command", block.input.get("code", "")))
                    if any(kw in cmd for kw in ("ros2 topic pub", "cmd_vel", "/drive")):
                        mvr = verify_motion(obs["stdout"])
                        if not mvr["success"]:
                            tracer.note(f"motion_not_verified: {mvr['evidence'][:80]}")
                            # motion 미확인도 inject — 물리 동작 goal에서 중요
                            _deferred_verify_messages.append(make_reflection_message(
                                f"Motion not verified: {mvr['evidence'][:150]}",
                                "replan",
                            ))

                memory.record_episode(block.name, out, ok)
                # working memory: last_action / last_result 업데이트
                memory.working.last_action = block.name
                memory.working.last_result = out[:50].replace("\n", " ")
                tracer.tool_use(
                    name=block.name,
                    inp_summary=str(block.input)[:100],
                    out_summary=out[:100],
                    ok=ok,
                )

            # ⑤ current_step 동기화 — todo in_progress 항목을 working memory에 반영
            _in_progress = todos.in_progress_items()
            if _in_progress:
                memory.working.current_step = _in_progress[0].content[:100]

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            # verifier inject — tool_results 조립 후 이어 붙임
            for _vm in _deferred_verify_messages:
                messages.append(_vm)

            if executor.is_finished:
                # 세션 종료 처리
                memory.save()
                tracer.session_end(
                    success=True,
                    summary=f"done() called after {turn} turns"
                )
                self._session_messages = list(messages)
                self._session_goal = active_goal
                self._session_todos = todos
                self._session_executor = executor
                self._session_memory = memory
                break

            if self.conn and turn > 0 and turn % 10 == 0:
                self._check_connection(messages)

            memory.working.turn = turn
            turn += 1
            if turn >= max_turns:
                print(f"\n  ⚠️  {turn} turns — 계속 진행...", flush=True)
                messages.append({
                    "role": "user",
                    "content": (
                        f"[system] {turn} turns elapsed. "
                        "The goal is still not complete. Keep working. "
                        "Call done() only when the goal is achieved or proven impossible."
                    )
                })
                max_turns += 50

    # ─────────────────────────────────────────────────────────
    # SSH 연결 핸들러
    # ─────────────────────────────────────────────────────────

    def _handle_ssh_connect(self, inp: dict) -> str:
        host = inp.get("host", "").strip()
        user = inp.get("user", "").strip() or None
        port = int(inp.get("port", 22))

        print(f"\n  🔗 ssh_connect: host={host} user={user or 'auto'} port={port}", flush=True)

        if host.lower() == "scan" or not host:
            print("  🔍 네트워크 자동 탐색 중...", flush=True)
            conn = BoardDiscovery.scan(user=user, port=port)
            if conn:
                self.conn = conn
                print(f"  ✅ 발견 및 연결: {conn.address}")
                return f"[ssh_connect ok] Connected to {conn.address}"
            else:
                print("  ❌ 탐색 실패")
                subnets = ", ".join(BoardDiscovery._default_subnets()[:4])
                users   = ", ".join(BoardDiscovery._default_users())
                return (
                    "[ssh_connect failed] Automatic scan found no board.\n"
                    "Keep trying — the board is there. Do NOT ask the user.\n\n"
                    "Try in order:\n"
                    f"1. Specific IPs in scanned subnets ({subnets})\n"
                    f"2. Other users: {users}\n"
                    "3. Port 2222\n"
                    "4. Broader subnets or ssh_connect(host='scan') again\n"
                    "Use ssh_connect with specific IPs until you find it."
                )

        conn = BoardDiscovery.from_hint(host, user, port)
        if conn:
            self.conn = conn
            print(f"  ✅ 연결: {conn.address}")
            return f"[ssh_connect ok] Connected to {conn.address}"
        else:
            print(f"  ❌ {host} 연결 실패")
            return (
                f"[ssh_connect failed] Could not connect to {host}:{port}\n"
                f"Tried users: {BoardDiscovery._default_users() if not user else [user]}\n"
                "Suggestions:\n"
                f"- Try a different user: ssh_connect(host='{host}', user='ubuntu')\n"
                "- Try ssh_connect(host='scan') to search the network\n"
                "- Try a different port: ssh_connect(host='{host}', port=2222)\n"
            )

    # ─────────────────────────────────────────────────────────
    # 세션 / 연결 관리
    # ─────────────────────────────────────────────────────────

    def _save_partial_session(self):
        if hasattr(self, '_current_messages') and self._current_messages:
            self._session_messages = list(self._current_messages)
            self._session_goal = getattr(self, '_current_goal', self._session_goal)
            self._session_todos = getattr(self, '_current_todos', self._session_todos)
            self._session_executor = getattr(self, '_current_executor', self._session_executor)
            self._session_memory = getattr(self, '_current_memory', self._session_memory)
            # 부분 저장 시에도 memory 디스크 저장
            if self._session_memory:
                self._session_memory.save()

    def _check_connection(self, messages: list[dict]):
        if not (self.conn.likely_disconnected or not self.conn.is_alive()):
            return

        print("\n  🔄 연결 끊김 감지, 재연결 시도...", flush=True)
        if self.conn.reconnect(max_attempts=3):
            print("  ✅ 재연결 성공")
            messages.append({
                "role": "user",
                "content": (
                    "[SSH reconnected]\n"
                    "Connection was lost and restored. "
                    "Check board state (running processes, temp files) before continuing."
                )
            })
        else:
            print("  ❌ 자동 재연결 실패.")
            self.conn = None
            messages.append({
                "role": "user",
                "content": (
                    "[SSH connection lost — reconnect failed]\n"
                    "Automatic reconnect (3 attempts) failed.\n"
                    "Use ssh_connect to re-establish connection before continuing."
                )
            })


# ─────────────────────────────────────────────────────────────
# Escalation Tracker
# ─────────────────────────────────────────────────────────────

class EscalationTracker:
    """
    트리거 조건 (OR):
      1. verify FAIL 2회 연속
      2. 직전 2개 tool_result에 동일한 하드웨어 실패 패턴
      3. 같은 bash 명령 3회 이상 반복 (polling 제외)
    """

    POLLING_KEYWORDS = ("hz", "echo --once", "topic echo", "ps aux", "is-active", "ping")

    FAIL_KEYWORDS = (
        "exit code 1", "exit code 255", "no data",
        "speed: 0.0", "speed=0.0", "0 publishers",
        "rc=-1", "timed out", "no response",
    )

    def __init__(self):
        self._verify_fail_streak: int = 0
        self._recent_results: list[str] = []
        self._bash_counter: dict[str, int] = {}

    def record_tool_results(self, tool_blocks: list, results: dict[str, str]) -> None:
        for block in tool_blocks:
            out = results.get(block.id, "")

            if block.name == "ssh_connect":
                continue

            if block.name == "verify":
                if "FAIL" in out or "fail" in out.lower():
                    self._verify_fail_streak += 1
                else:
                    self._verify_fail_streak = 0

            if block.name == "bash":
                cmd = block.input.get("command", "")
                if not any(kw in cmd for kw in self.POLLING_KEYWORDS):
                    self._bash_counter[cmd] = self._bash_counter.get(cmd, 0) + 1

            if out:
                self._recent_results.append(out.lower()[:300])
                if len(self._recent_results) > 4:
                    self._recent_results.pop(0)

    def should_escalate(self) -> tuple[bool, str]:
        if self._verify_fail_streak >= 2:
            return True, f"verify FAIL {self._verify_fail_streak}회 연속"

        if len(self._recent_results) >= 2:
            last_two = self._recent_results[-2:]
            for kw in self.FAIL_KEYWORDS:
                if all(kw in r for r in last_two):
                    return True, f"동일 실패 패턴 반복: '{kw}'"

        for cmd, count in self._bash_counter.items():
            if count >= 3:
                return True, f"bash 명령 {count}회 반복: '{cmd[:60]}'"

        return False, ""

    def get_recent_results(self) -> list[str]:
        """최근 tool_result 목록 반환 (reflection.py용 public interface)."""
        return list(self._recent_results)

    def reset_escalation(self) -> None:
        self._verify_fail_streak = 0
        self._bash_counter.clear()
        self._recent_results.clear()


# ─────────────────────────────────────────────────────────────
# 출력 헬퍼
# ─────────────────────────────────────────────────────────────

def _print_thinking(text: str) -> None:
    lines = text.strip().splitlines()
    first = lines[0][:120] if lines else ""
    total_chars = len(text)
    print(f"\n  🧠 thinking ({total_chars}ch): {first}", flush=True)
    if len(lines) > 1:
        print(f"     ... ({len(lines)} lines)", flush=True)


def _extract_known_context(messages: list[dict]) -> str:
    context_lines = []
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
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
