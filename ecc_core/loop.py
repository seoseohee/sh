"""
ecc_core/loop.py — ECC 에이전트 루프 v3
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
from .verifier import verify_execution, verify_motion, parse_error_feedback


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


class SubagentRole:
    EXPLORER = "explorer"
    SETUP    = "setup"
    VERIFIER = "verifier"


def _subagent_config(role, conn, context):
    base_ssh  = f"SSH: {conn.user}@{conn.host}:{conn.port}"
    known_ctx = f"\nAlready known:\n{context}" if context else ""

    report_tool = {
        "name": "report",
        "description": "Task complete. Return findings/results to the main agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "string",
                    "description": (
                        "Complete findings or results. "
                        "Include specific values: device paths, IP addresses, "
                        "parameter names/values, topic names, PASS/FAIL status."
                    )
                }
            },
            "required": ["findings"]
        }
    }

    all_tools = get_tool_definitions()

    if role == SubagentRole.SETUP:
        system = (
            "You are a SETUP subagent for ECC.\n"
            "Install packages, write config files, start/restart services, configure the board.\n"
            "You CAN execute commands that modify system state.\n"
            "Call report() with: what was done, current state, any issues.\n"
            f"{base_ssh}{known_ctx}"
        )
        tools = [t for t in all_tools if t["name"] not in ("subagent", "done")] + [report_tool]

    elif role == SubagentRole.VERIFIER:
        system = (
            "You are a VERIFIER subagent for ECC.\n"
            "Verify the system using verify(), probe(), and read-only bash. Do NOT modify state.\n"
            "Report PASS/FAIL with concrete evidence. Call report() with structured results.\n"
            f"{base_ssh}{known_ctx}"
        )
        allowed = {"bash", "bash_wait", "read", "glob", "grep", "probe", "verify", "todo"}
        tools   = [t for t in all_tools if t["name"] in allowed] + [report_tool]

    else:  # EXPLORER
        system = (
            "You are an EXPLORER subagent for ECC. Investigate and call report().\n"
            "Batch independent commands. Do NOT modify system state.\n"
            "Include specific values: paths, addresses, parameters, versions.\n"
            f"{base_ssh}{known_ctx}"
        )
        tools = [t for t in all_tools if t["name"] not in ("subagent", "done", "write")] + [report_tool]

    return system, tools


def run_subagent(goal, context, conn, client, memory, verbose=False, role=SubagentRole.EXPLORER):
    system, tools = _subagent_config(role, conn, context)
    todos    = TodoManager()
    executor = ToolExecutor(conn, todos, memory=memory, verbose=verbose)
    messages = [{"role": "user", "content": goal}]
    max_turns = _env_int("ECC_SUBAGENT_TURNS", 40)
    turn = 0
    PARALLEL = {"bash", "bash_wait", "script", "read", "write", "glob", "grep", "probe", "verify", "todo"}

    while True:
        resp = client.messages.create(
            model=_main_model(), max_tokens=4096,
            system=system, tools=tools, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        findings = ""
        finished = False
        all_results = {}

        serial_blocks   = [b for b in resp.content if b.type == "tool_use" and b.name not in PARALLEL]
        parallel_blocks = [b for b in resp.content if b.type == "tool_use" and b.name in PARALLEL]

        for block in serial_blocks:
            if block.name == "report":
                findings = block.input.get("findings", "")
                all_results[block.id] = "reported"
                finished = True
            else:
                all_results[block.id] = executor.execute(block.name, block.input)

        if parallel_blocks and not finished:
            with ThreadPoolExecutor(max_workers=min(len(parallel_blocks), 8)) as pool:
                futures = {pool.submit(executor.execute, b.name, b.input): b.id for b in parallel_blocks}
                for future in as_completed(futures):
                    bid = futures[future]
                    try:
                        all_results[bid] = future.result()
                    except Exception as e:
                        all_results[bid] = f"[error] {e}"

        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            out = all_results.get(block.id, "[error] no result")
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if finished:
            return findings

        if resp.stop_reason == "end_turn" and not any(b.type == "tool_use" for b in resp.content):
            messages.append({"role": "user", "content": "[system] Call report() with your findings."})
            continue

        turn += 1
        if turn >= max_turns:
            messages.append({"role": "user", "content": f"[system] {turn} turns. Call report() now."})
            max_turns += 20

    return "(subagent: no report)"


class AgentLoop:

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.client  = anthropic.Anthropic()
        self.conn    = None
        self._session_messages  = []
        self._session_goal      = ""
        self._session_todos     = None
        self._session_executor  = None
        self._session_memory    = None

    @staticmethod
    def _is_followup(goal, has_session):
        if not has_session:
            return False
        stripped = goal.strip()
        FOLLOWUP_PREFIXES = ("/continue", "/resume", "/more", "/add", "/also")
        return any(stripped.lower().startswith(p) for p in FOLLOWUP_PREFIXES)

    def run(self, goal, max_turns=100):
        print(f"\n{'═'*60}\n  🎯 {goal[:80]}\n{'═'*60}")

        if self.conn and not self.conn.is_alive():
            print("  ⚠️  이전 연결이 끊어짐.")
            self.conn = None

        is_followup = self._is_followup(goal, bool(self._session_messages))
        active_goal = self._session_goal if is_followup else goal

        memory = (
            self._session_memory
            if (is_followup and self._session_memory)
            else ECCMemory(conn_address=self.conn.address if self.conn else "")
        )
        memory.working.goal = active_goal
        tracer = Tracer(goal=active_goal[:80])

        if is_followup:
            todos    = self._session_todos or TodoManager()
            executor = self._session_executor or ToolExecutor(self.conn, todos, memory=memory, verbose=self.verbose)
            executor.conn     = self.conn
            executor.memory   = memory
            executor.is_finished = False
            messages = self._session_messages + [{"role": "user", "content": f"[User follow-up] {goal}"}]
            print(f"  🔁 이전 세션 이어받기 ({len(self._session_messages)}개 메시지)", flush=True)
        else:
            todos    = TodoManager()
            executor = ToolExecutor(self.conn, todos, memory=memory, verbose=self.verbose)
            messages = [{"role": "user", "content": goal}]

        self._current_goal     = active_goal
        self._current_todos    = todos
        self._current_executor = executor
        self._current_messages = messages
        self._current_memory   = memory

        system     = build_system_prompt()
        model      = _main_model()
        max_tokens = _main_max_tokens()
        if _thinking_enabled():
            max_tokens = max(max_tokens, _thinking_budget() + 4096)

        escalation         = EscalationTracker()
        turn               = 0
        _retry_count       = 0
        _last_retry_reason = ""

        while True:
            if should_compact(messages):
                facts    = memory.get_persistent_facts()
                messages = compact(messages, active_goal, todos.format_for_llm(), self.client, persistent_facts=facts)
                tracer.note("context compacted")

            conn_status = f"[Connected: {self.conn.address}]" if self.conn else "[Not connected]"
            mem_ctx     = memory.to_system_context()
            nag         = todos.format_nag()
            system_with_state = (
                system
                + f"\n\nCurrent connection: {conn_status}"
                + (f"\n\n{mem_ctx}" if mem_ctx else "")
                + (f"\n\n{nag}"     if nag     else "")
            )

            try:
                escalate, reason  = escalation.should_escalate()
                turn_model        = _escalate_model() if escalate else model
                turn_thinking     = escalate or _thinking_enabled()
                turn_max_tokens   = max(max_tokens, _thinking_budget() + 4096) if (turn_thinking and not _supports_adaptive(turn_model)) else max_tokens

                if escalate:
                    _decision   = classify_failure(escalation.get_recent_results())
                    _reflection = generate_reflection(messages, active_goal, _decision, self.client, model=model)
                    messages.append(make_reflection_message(_reflection, _decision))
                    tracer.reflection(_decision, _reflection)
                    print(f"\n  🔺 Escalate → {turn_model} ({reason})", flush=True)

                    from .reflection import ReplanDecision
                    if _decision == ReplanDecision.RETRY_SAME_TASK:
                        _retry_count = _retry_count + 1 if reason == _last_retry_reason else 1
                        _last_retry_reason = reason
                        if _retry_count >= 3:
                            messages.append({"role": "user", "content": "[system] RETRY limit (3x). Switch strategy or call done(success=false)."})
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

                t0   = time.monotonic()
                resp = self.client.messages.create(**create_kwargs)
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
                wait = 60
                print(f"\n  ⏳ Rate limit — {wait}초 대기...", flush=True)
                time.sleep(wait)
                continue
            except anthropic.BadRequestError as e:
                err = str(e).lower()
                if any(kw in err for kw in ("context", "too long", "too many token", "input length", "prompt_too_long")):
                    facts    = memory.get_persistent_facts()
                    messages = compact(messages, active_goal, todos.format_for_llm(), self.client, persistent_facts=facts)
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
                print("\n  ⚠️  done() 없이 멈춤.", flush=True)
                messages.append({"role": "user", "content": "[system] Call done() or continue working."})
                continue

            tool_blocks = [b for b in resp.content if b.type == "tool_use"]

            PARALLEL_TOOLS = {
                "bash", "bash_wait", "script", "read", "write", "glob", "grep",
                "probe", "verify", "todo", "serial_open", "serial_send", "serial_close", "remember",
            }

            serial_blocks   = [b for b in tool_blocks if b.name not in PARALLEL_TOOLS or self.conn is None]
            parallel_blocks = [b for b in tool_blocks if b.name in PARALLEL_TOOLS and self.conn is not None]

            serial_results = {}
            for block in serial_blocks:
                if block.name == "ssh_connect":
                    out = self._handle_ssh_connect(block.input)
                    executor.conn = self.conn
                    if self.conn:
                        memory.update_connection(self.conn.address)
                elif self.conn is None and block.name not in {"bash","bash_wait","read","write","glob","grep","todo","done","ask_user"}:
                    out = "[no connection] Call ssh_connect first."
                elif block.name == "subagent":
                    known = _extract_known_context(messages)
                    role  = block.input.get("role", SubagentRole.EXPLORER)
                    out   = run_subagent(
                        goal=block.input.get("goal", ""),
                        context=block.input.get("context", known),
                        conn=self.conn, client=self.client, memory=memory,
                        verbose=self.verbose, role=role,
                    )
                elif block.name == "ask_user":
                    out = executor.execute("ask_user", block.input)
                else:
                    task_hint = f"{block.name} {str(block.input)[:80]}"
                    feasible, reason = memory.can_execute(task_hint)
                    if not feasible:
                        out = f"[can_execute blocked] {reason}\nRun probe() first."
                    else:
                        out = executor.execute(block.name, block.input)
                serial_results[block.id] = out

            parallel_results = {}
            if parallel_blocks:
                with ThreadPoolExecutor(max_workers=min(len(parallel_blocks), 8)) as pool:
                    futures = {pool.submit(executor.execute, b.name, b.input): b.id for b in parallel_blocks}
                    for future in as_completed(futures):
                        bid = futures[future]
                        try:
                            parallel_results[bid] = future.result()
                        except Exception as e:
                            parallel_results[bid] = f"[error] {e}"

            all_results = {**serial_results, **parallel_results}

            # EmbedAgent compiler feedback loop: 에러 → 구조화 annotation
            _verify_annotations = {}

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
                        ann = f"\n\n[Verifier] reason={vr['reason']}\nevidence: {vr['evidence'][:120]}\n"
                        fb  = vr.get("feedback")
                        if fb:
                            ann += (f"error_type: {fb['error_type']}\n"
                                    f"root_cause: {fb['root_cause']}\n"
                                    f"suggested_fix: {fb['suggested_fix']}\n"
                                    f"retry_safe: {fb['retry_safe']}\n")
                        ann += f"→ Next: {recovery.note}"
                        _verify_annotations[block.id] = ann

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
                            _verify_annotations[block.id] = ann
                    elif not ok:
                        fb = parse_error_feedback(out)
                        if fb:
                            _verify_annotations[block.id] = (
                                f"\n\n[Error feedback]\n"
                                f"error_type: {fb['error_type']}\n"
                                f"root_cause: {fb['root_cause']}\n"
                                f"suggested_fix: {fb['suggested_fix']}\n"
                                f"retry_safe: {fb['retry_safe']}"
                            )

                memory.record_episode(block.name, out, ok)
                memory.working.last_action = block.name
                memory.working.last_result = out[:50].replace("\n", " ")
                tracer.tool_use(name=block.name, inp_summary=str(block.input)[:100], out_summary=out[:100], ok=ok)

            escalation.record_tool_results(tool_blocks, all_results)

            _in_prog = todos.in_progress_items()
            if _in_prog:
                memory.working.current_step = _in_prog[0].content[:100]

            tool_results = []
            for block in tool_blocks:
                out = all_results.get(block.id, "[error] no result")
                ann = _verify_annotations.get(block.id, "")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": out + ann})

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if executor.is_finished:
                memory.save()
                tracer.session_end(success=True, summary=f"done() after {turn} turns")
                self._session_messages  = list(messages)
                self._session_goal      = active_goal
                self._session_todos     = todos
                self._session_executor  = executor
                self._session_memory    = memory
                break

            if self.conn and turn > 0 and turn % 10 == 0:
                self._check_connection(messages)

            memory.working.turn = turn
            turn += 1
            if turn >= max_turns:
                print(f"\n  ⚠️  {turn} turns — 계속 진행...", flush=True)
                messages.append({"role": "user", "content": f"[system] {turn} turns. Keep working. Call done() only when finished."})
                max_turns += 50

    def _handle_ssh_connect(self, inp):
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
            subnets = ", ".join(BoardDiscovery._default_subnets()[:4])
            users   = ", ".join(BoardDiscovery._default_users())
            return (
                f"[ssh_connect failed] No board found.\n"
                f"Try: specific IPs ({subnets}), users ({users}), port 2222."
            )

        conn = BoardDiscovery.from_hint(host, user, port)
        if conn:
            self.conn = conn
            print(f"  ✅ 연결: {conn.address}")
            return f"[ssh_connect ok] Connected to {conn.address}"
        return (
            f"[ssh_connect failed] Could not connect to {host}:{port}\n"
            f"Try ssh_connect(host='scan') or a different user/port."
        )

    def _save_partial_session(self):
        if hasattr(self, '_current_messages') and self._current_messages:
            self._session_messages  = list(self._current_messages)
            self._session_goal      = getattr(self, '_current_goal',     self._session_goal)
            self._session_todos     = getattr(self, '_current_todos',    self._session_todos)
            self._session_executor  = getattr(self, '_current_executor', self._session_executor)
            self._session_memory    = getattr(self, '_current_memory',   self._session_memory)
            if self._session_memory:
                self._session_memory.save()

    def _check_connection(self, messages):
        if not (self.conn.likely_disconnected or not self.conn.is_alive()):
            return
        print("\n  🔄 연결 끊김, 재연결 시도...", flush=True)
        if self.conn.reconnect(max_attempts=3):
            print("  ✅ 재연결 성공")
            messages.append({"role": "user", "content": "[SSH reconnected] Check board state before continuing."})
        else:
            print("  ❌ 재연결 실패.")
            self.conn = None
            messages.append({"role": "user", "content": "[SSH lost] Use ssh_connect to reconnect."})


class EscalationTracker:
    POLLING_KEYWORDS = ("hz", "echo --once", "topic echo", "ps aux", "is-active", "ping")
    FAIL_KEYWORDS    = ("exit code 1", "exit code 255", "no data", "speed: 0.0", "speed=0.0",
                        "0 publishers", "rc=-1", "timed out", "no response")

    def __init__(self):
        self._verify_fail_streak = 0
        self._recent_results     = []
        self._bash_counter       = {}

    def record_tool_results(self, tool_blocks, results):
        for block in tool_blocks:
            out = results.get(block.id, "")
            if block.name == "ssh_connect":
                continue
            if block.name == "verify":
                self._verify_fail_streak = self._verify_fail_streak + 1 if ("FAIL" in out or "fail" in out.lower()) else 0
            if block.name == "bash":
                cmd = block.input.get("command", "")
                if not any(kw in cmd for kw in self.POLLING_KEYWORDS):
                    self._bash_counter[cmd] = self._bash_counter.get(cmd, 0) + 1
            if out:
                self._recent_results.append(out.lower()[:300])
                if len(self._recent_results) > 4:
                    self._recent_results.pop(0)

    def should_escalate(self):
        if self._verify_fail_streak >= 2:
            return True, f"verify FAIL {self._verify_fail_streak}회 연속"
        if len(self._recent_results) >= 2:
            last_two = self._recent_results[-2:]
            for kw in self.FAIL_KEYWORDS:
                if all(kw in r for r in last_two):
                    return True, f"동일 실패 패턴: '{kw}'"
        for cmd, count in self._bash_counter.items():
            if count >= 3:
                return True, f"bash {count}회 반복: '{cmd[:60]}'"
        return False, ""

    def get_recent_results(self):
        return list(self._recent_results)

    def reset_escalation(self):
        self._verify_fail_streak = 0
        self._bash_counter.clear()
        self._recent_results.clear()


def _print_thinking(text):
    lines = text.strip().splitlines()
    first = lines[0][:120] if lines else ""
    print(f"\n  🧠 thinking ({len(text)}ch): {first}", flush=True)
    if len(lines) > 1:
        print(f"     ... ({len(lines)} lines)", flush=True)


def _extract_known_context(messages):
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
