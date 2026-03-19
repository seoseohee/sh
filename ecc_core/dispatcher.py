"""ecc_core/dispatcher.py — Tool execution dispatcher.

Tool execution responsibilities separated from AgentLoop:
  - parallel / serial tool branching
  - ssh_connect handling
  - subagent routing (SubagentRole)
  - _handle_ssh_connect, _check_connection

Changelog:
  v2 — [Fix 2] run_subagent 절대 상한선 추가
         기존: max_turns += 20 반복으로 이론상 무한 실행 가능
         수정: ECC_SUBAGENT_MAX_TURNS_ABSOLUTE (default: 200)로 절대 상한 적용
               초과 시 즉시 "(subagent: hard turn limit)" 반환
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from .connection import BoardConnection, BoardDiscovery
from .memory     import ECCMemory
from .executor   import ToolExecutor
from .todo       import TodoManager
from .tool_schemas import get_tool_definitions


# ── Subagent roles ──────────────────────────────────────────

class SubagentRole:
    """
    EXPLORER  — Exploration/investigation only. No state modification.
    SETUP     — Can install, configure, write files.
    VERIFIER  — specialized for verify/probe/read-only bash.
    """
    EXPLORER = "explorer"
    SETUP    = "setup"
    VERIFIER = "verifier"


def _subagent_config(role: str, conn: BoardConnection, context: str) -> tuple:
    """Return (system_prompt, tools) per role."""
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


def run_subagent(
    goal:    str,
    context: str,
    conn:    BoardConnection,
    client,
    memory:  ECCMemory,
    verbose: bool = False,
    role:    str  = SubagentRole.EXPLORER,
) -> str:
    """Role-based subagent execution."""
    import os
    from .tool_schemas import get_tool_definitions

    def _env_int(k, d):
        try: return int(os.environ.get(k, d))
        except: return d

    def _main_model():
        return os.environ.get("ECC_MODEL", "claude-sonnet-4-6")

    system, tools = _subagent_config(role, conn, context)
    todos         = TodoManager()
    executor      = ToolExecutor(conn, todos, memory=memory, verbose=verbose)
    messages      = [{"role": "user", "content": goal}]
    max_turns     = _env_int("ECC_SUBAGENT_TURNS", 40)

    # [Fix 2] 절대 상한선: max_turns += 20 무한 반복 방지
    # 개별 확장은 허용하되, 이 값을 초과하면 즉시 중단
    hard_limit    = _env_int("ECC_SUBAGENT_MAX_TURNS_ABSOLUTE", 200)

    turn          = 0

    PARALLEL = {
        "bash", "bash_wait", "script", "read", "write",
        "glob", "grep", "probe", "verify", "todo",
    }

    while True:
        # [Fix 2] 절대 상한 체크 — max_turns 확장 여부와 무관하게 강제 종료
        if turn >= hard_limit:
            print(
                f"\n  ⚠️  subagent hard turn limit reached ({hard_limit}). "
                "Forcing return without report.",
                flush=True,
            )
            return f"(subagent: hard turn limit {hard_limit} reached — no report)"

        resp = client.messages.create(
            model=_main_model(), max_tokens=_env_int("ECC_SUBAGENT_MAX_TOKENS", 4096),
            system=system, tools=tools, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        findings    = ""
        finished    = False
        all_results: dict[str, str] = {}

        serial_blocks   = [b for b in resp.content if b.type == "tool_use" and b.name not in PARALLEL]
        parallel_blocks = [b for b in resp.content if b.type == "tool_use" and b.name in PARALLEL]

        for block in serial_blocks:
            if block.name == "report":
                findings            = block.input.get("findings", "")
                all_results[block.id] = "reported"
                finished            = True
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
            # [Fix 2] 확장은 허용하되 hard_limit이 최종 방어선
            max_turns += 20

    return "(subagent: no report)"


# ── ToolDispatcher ─────────────────────────────────────────

PARALLEL_TOOLS = {
    "bash", "bash_wait", "script",
    "read", "write", "glob", "grep",
    "probe", "verify", "todo",
    "serial_open", "serial_send", "serial_close",
    "remember",
}


class ToolDispatcher:
    """
    Receive tool_blocks, execute in parallel/serial, return all_results.

    Responsibilities:
      - PARALLEL_TOOLS branching
      - ssh_connect handling
      - subagent routing
      - can_execute affordance check
      - SSH reconnect detection
    """

    def __init__(self, agent_loop):
        self._loop = agent_loop  # access conn, client, verbose

    @property
    def conn(self) -> "BoardConnection | None":
        return self._loop.conn

    def dispatch(
        self,
        tool_blocks: list,
        executor:    ToolExecutor,
        memory:      ECCMemory,
        messages:    list[dict],
    ) -> dict[str, str]:
        """Execute tool_blocks → {block.id: result_str}."""
        serial_blocks   = [b for b in tool_blocks if b.name not in PARALLEL_TOOLS or self.conn is None]
        parallel_blocks = [b for b in tool_blocks if b.name in PARALLEL_TOOLS and self.conn is not None]

        serial_results: dict[str, str] = {}
        for block in serial_blocks:
            serial_results[block.id] = self._dispatch_one(block, executor, memory, messages)

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

        return {**serial_results, **parallel_results}

    def _dispatch_one(
        self,
        block,
        executor:  ToolExecutor,
        memory:    ECCMemory,
        messages:  list[dict],
    ) -> str:
        if block.name == "ssh_connect":
            out = self.handle_ssh_connect(block.input)
            executor.conn = self.conn
            if self.conn:
                memory.update_connection(self.conn.address)
            return out

        if self.conn is None and block.name not in {
            "bash", "bash_wait", "read", "write", "glob", "grep",
            "todo", "done", "ask_user",
        }:
            return "[no connection] Call ssh_connect first."

        if block.name == "subagent":
            from .loop import _extract_known_context
            known = _extract_known_context(messages)
            role  = block.input.get("role", SubagentRole.EXPLORER)
            return run_subagent(
                goal    = block.input.get("goal", ""),
                context = block.input.get("context", known),
                conn    = self.conn,
                client  = self._loop.client,
                memory  = memory,
                verbose = self._loop.verbose,
                role    = role,
            )

        if block.name == "ask_user":
            return executor.execute("ask_user", block.input)

        task_hint = f"{block.name} {str(block.input)[:80]}"
        feasible, reason = memory.can_execute(task_hint)
        if not feasible:
            return f"[can_execute blocked] {reason}\nRun probe() first."

        return executor.execute(block.name, block.input)

    def handle_ssh_connect(self, inp: dict) -> str:
        host = inp.get("host", "").strip()
        user = inp.get("user", "").strip() or None
        port = int(inp.get("port", 22))
        print(f"\n  🔗 ssh_connect: host={host} user={user or 'auto'} port={port}", flush=True)

        if host.lower() == "scan" or not host:
            loop = self._loop
            if hasattr(loop, '_session') and loop._session._saved_memory:
                profile = loop._session._saved_memory.get_ssh_profile()
                if profile:
                    cached_host = profile.get("host_port", "").split(":")[0]
                    cached_user = profile.get("user", "")
                    cached_port = int(profile.get("host_port", ":22").split(":")[-1]) if ":" in profile.get("host_port", "") else 22
                    if cached_host:
                        print(f"  ⚡ Trying SSH profile cache: {cached_user}@{cached_host}:{cached_port}", flush=True)
                        conn = BoardDiscovery.from_hint(cached_host, cached_user, cached_port)
                        if conn:
                            self._loop.conn = conn
                            print(f"  ✅ Cache connect succeeded: {conn.address}")
                            return f"[ssh_connect ok] Connected to {conn.address} (cached)"

            print("  🔍 Scanning network......", flush=True)
            conn = BoardDiscovery.scan(user=user, port=port)
            if conn:
                self._loop.conn = conn
                print(f"  ✅ Discovered and connected: {conn.address}")
                return f"[ssh_connect ok] Connected to {conn.address}"
            subnets = ", ".join(BoardDiscovery._default_subnets()[:4])
            users   = ", ".join(BoardDiscovery._default_users())
            return (
                f"[ssh_connect failed] No board found.\n"
                f"Try: specific IPs ({subnets}), users ({users}), port 2222."
            )

        conn = BoardDiscovery.from_hint(host, user, port)
        if conn:
            self._loop.conn = conn
            print(f"  ✅ Connected: {conn.address}")
            return f"[ssh_connect ok] Connected to {conn.address}"
        return (
            f"[ssh_connect failed] Could not connect to {host}:{port}\n"
            "Try ssh_connect(host='scan') or a different user/port."
        )

    def check_connection(self, messages: list[dict]) -> None:
        if not self.conn:
            return
        if not self.conn.likely_disconnected:
            return
        if self.conn.is_alive():
            self.conn._consecutive_failures = 0
            return
        print("\n  🔄 Connection lost, attempting reconnect......", flush=True)
        if self.conn.reconnect():  # max_attempts는 ECC_SSH_RECONNECT_ATTEMPTS env로 제어
            print("  ✅ Reconnect succeeded")
            messages.append({"role": "user", "content": "[SSH reconnected] Check board state."})
        else:
            print("  ❌ Reconnect failed..")
            self._loop.conn = None
            messages.append({"role": "user", "content": "[SSH lost] Use ssh_connect to reconnect."})