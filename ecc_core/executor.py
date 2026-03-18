"""
ecc_core/executor.py

Executes tool_use requests from the LLM as actual board commands.

Changelog:
  v2 — Fixed _ask_user defined outside class (AttributeError)
       Added background task completion wait on done() (race condition fix)
"""

import json
import os
import subprocess
import threading
import uuid as _uuid_mod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .connection import BoardConnection, ExecResult
from .tools import is_dangerous, PROBE_COMMANDS, VERIFY_COMMANDS
from .todo import TodoManager

if TYPE_CHECKING:
    pass


@dataclass
class _BgTask:
    task_id: str
    cmd: str
    result: "ExecResult | None" = field(default=None)
    done: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_result(self, r: "ExecResult") -> None:
        with self._lock:
            self.result = r
            self.done = True

    def wait(self, timeout: float) -> bool:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.done:
                return True
            time.sleep(0.2)
        return self.done


class ToolExecutor:
    """
    Dispatches tool names to actual operations.
    Sets is_finished = True when done() is called.
    """

    def __init__(self, conn, todos: TodoManager, memory=None, verbose: bool = False):
        self.conn = conn
        self.todos = todos
        self.memory = memory
        self.verbose = verbose
        self.is_finished = False
        self._bg_tasks: dict[str, _BgTask] = {}
        self._serial_sessions: dict[str, dict] = {}

    def execute(self, tool_name: str, tool_input: dict) -> str:
        handler = getattr(self, f"_{tool_name}", None)
        if handler is None:
            return (
                f"[error] Unknown tool: {tool_name}\n"
                f"Available tools: "
                + ", ".join(
                    n[1:] for n in dir(self)
                    if n.startswith("_") and not n.startswith("__")
                    and callable(getattr(self, n))
                    and n[1:] not in ("bg_tasks", "serial_sessions")
                )
            )
        return handler(tool_input)

    # ─── bash ──────────────────────────────────────────────────

    def _bash(self, inp: dict) -> str:
        cmd = inp["command"]
        timeout = inp.get("timeout", 30)
        desc = inp.get("description", "")
        background = inp.get("background", False)

        _print_tool("bash", f"{cmd[:100]}", desc)

        if is_dangerous(cmd):
            return "[blocked] Dangerous command rejected."

        safety_warn = self._physical_safety_check(cmd)
        if safety_warn:
            return f"[safety_blocked] {safety_warn}"

        if background:
            task_id = _uuid_mod.uuid4().hex[:8]
            task = _BgTask(task_id=task_id, cmd=cmd)
            self._bg_tasks[task_id] = task

            def _run():
                r = self.conn.run(cmd, timeout=timeout)
                task.set_result(r)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            print(f"    Background task_id={task_id}", flush=True)
            return (
                f"[background] task_id={task_id}\n"
                f"Command running in background. "
                f"Use bash_wait(task_id='{task_id}') to retrieve result."
            )

        if self.conn is None:
            result = _local_run(cmd, timeout=timeout)
        else:
            result = self.conn.run(cmd, timeout=timeout)

            if (
                not result.ok
                and result.rc == -1
                and "timeout" in result.stderr.lower()
                and not background
            ):
                retry_timeout = min(timeout * 2, 120)
                print(f"    Timeout detected — retrying with {retry_timeout}s...", flush=True)
                result = self.conn.run(cmd, timeout=retry_timeout)

        _print_result(result)
        return result.to_tool_result()

    # ─── bash_wait ─────────────────────────────────────────────

    def _bash_wait(self, inp: dict) -> str:
        task_id = inp["task_id"]
        wait_timeout = inp.get("timeout", 120)
        desc = inp.get("description", "")

        _print_tool("bash_wait", f"task_id={task_id}", desc)

        task = self._bg_tasks.get(task_id)
        if task is None:
            return f"[error] task_id '{task_id}' not found. Valid IDs: {list(self._bg_tasks.keys())}"

        finished = task.wait(timeout=wait_timeout)
        if not finished:
            return (
                f"[timeout] task_id={task_id} still running after {wait_timeout}s.\n"
                "Call bash_wait again with a longer timeout, or continue other work."
            )

        result = task.result
        del self._bg_tasks[task_id]
        _print_result(result)
        return result.to_tool_result()

    # ─── script ────────────────────────────────────────────────

    def _script(self, inp: dict) -> str:
        code = inp["code"]
        interpreter = inp.get("interpreter", "bash")
        timeout = inp.get("timeout", 60)
        desc = inp.get("description", "")

        lines = code.strip().splitlines()
        _print_tool("script", f"[{interpreter}] {len(lines)} lines", desc)

        if self.verbose:
            preview = "\n    ".join(lines[:6])
            suffix = "\n    ..." if len(lines) > 6 else ""
            print(f"    {preview}{suffix}")

        result = self.conn.upload_and_run(code, interpreter=interpreter, timeout=timeout)
        _print_result(result)
        return result.to_tool_result()

    # ─── read ──────────────────────────────────────────────────

    def _read(self, inp: dict) -> str:
        path = inp["path"]
        head = inp.get("head_lines", 0)
        tail = inp.get("tail_lines", 0)

        _print_tool("read", path)

        if head > 0:
            cmd = f"head -n {head} {path}"
        elif tail > 0:
            cmd = f"tail -n {tail} {path}"
        else:
            cmd = f"cat {path}"

        if self.conn is None:
            result = _local_run(cmd, timeout=15)
        else:
            result = self.conn.run(cmd, timeout=15)
        _print_result(result)
        return result.to_tool_result()

    # ─── write ─────────────────────────────────────────────────

    def _write(self, inp: dict) -> str:
        path = inp["path"]
        content = inp["content"]
        mode = inp.get("mode", "")

        _print_tool("write", path)

        if self.conn is None:
            result = _local_write(path, content, mode)
            _print_result(result)
            return result.to_tool_result()

        result = self.conn.upload_and_run(
            f"mkdir -p $(dirname {path})",
            interpreter="bash",
            timeout=10
        )

        result = self.conn.upload_and_run(
            content,
            interpreter=f"bash -c 'cat > {path}'",
            timeout=15
        )

        if result.ok and mode:
            self.conn.run(f"chmod {mode} {path}", timeout=5)

        _print_result(result)
        return result.to_tool_result()

    # ─── glob ──────────────────────────────────────────────────

    def _glob(self, inp: dict) -> str:
        pattern = inp["pattern"]
        base = inp.get("base_dir", "/")

        _print_tool("glob", pattern)

        if pattern.startswith("/"):
            cmd = f"find / -path '{pattern}' 2>/dev/null | head -50"
        else:
            cmd = f"find {base} -path '*{pattern}*' 2>/dev/null | head -50"

        if self.conn is None:
            result = _local_run(cmd, timeout=20)
        else:
            result = self.conn.run(cmd, timeout=20)
        _print_result(result)
        return result.to_tool_result()

    # ─── grep ──────────────────────────────────────────────────

    def _grep(self, inp: dict) -> str:
        pattern = inp["pattern"]
        path = inp["path"]
        flags = inp.get("flags", "-rn")
        max_results = inp.get("max_results", 50)

        _print_tool("grep", f'"{pattern}" in {path}')

        cmd = (
            f"(rg {flags} --max-count {max_results} '{pattern}' {path} 2>/dev/null) "
            f"|| (grep {flags} --max-count {max_results} '{pattern}' {path} 2>/dev/null)"
        )
        if self.conn is None:
            result = _local_run(cmd, timeout=20)
        else:
            result = self.conn.run(cmd, timeout=20)
        _print_result(result)
        return result.to_tool_result()

    # ─── probe ─────────────────────────────────────────────────

    def _probe(self, inp: dict) -> str:
        target = inp["target"]

        _print_tool("probe", f"[{target}]", "hardware/env detection")

        from .tools import probe_registry
        cmd = probe_registry.get(target)
        if not cmd:
            from .tools import PROBE_COMMANDS
            cmd = PROBE_COMMANDS.get(target)
        if not cmd:
            available = probe_registry.list_targets()
            return f"[error] Unknown probe target: {target}. Available: {available}"

        timeout = 60 if target == "parallel_scan" else 45
        result = self.conn.run(cmd, timeout=timeout)
        _print_result(result)
        return result.to_tool_result()

    # ─── serial_open ───────────────────────────────────────────

    def _serial_open(self, inp: dict) -> str:
        port     = inp["port"]
        baudrate = inp.get("baudrate", 115200)
        timeout  = inp.get("timeout", 1.0)
        desc     = inp.get("description", "")

        _print_tool("serial_open", f"{port} @ {baudrate}", desc)

        session_id = _uuid_mod.uuid4().hex[:8]
        self._serial_sessions[session_id] = {
            "port":     port,
            "baudrate": baudrate,
            "timeout":  timeout,
            "desc":     desc,
            "history":  [],
        }

        check = self.conn.run(
            f"python3 -c \""
            f"import serial; s=serial.Serial('{port}', {baudrate}, timeout={timeout}); "
            f"s.close(); print('ok')\"",
            timeout=10
        )
        if not check.ok:
            del self._serial_sessions[session_id]
            _print_result(check)
            return (
                f"[serial_open failed] {port} @ {baudrate}\n"
                f"{check.to_tool_result()}\n"
                "Hints:\n"
                "- Check port path: probe(target='hw')\n"
                "- Check permissions: bash('ls -la /dev/ttyACM* /dev/ttyUSB*')\n"
                "- Install pyserial: bash('pip3 install pyserial --break-system-packages')"
            )

        print(f"    serial session_id={session_id}  ({port} @ {baudrate})", flush=True)
        return (
            f"[serial_open ok] session_id={session_id}\n"
            f"port={port} baudrate={baudrate} timeout={timeout}\n"
            f"Use serial_send(session_id='{session_id}', data=...) to communicate."
        )

    # ─── serial_send ───────────────────────────────────────────

    def _serial_send(self, inp: dict) -> str:
        session_id = inp["session_id"]
        data       = inp["data"]
        expect     = inp.get("expect", "")
        timeout    = inp.get("timeout", 2.0)
        hex_encode = inp.get("hex_encode", False)

        _print_tool("serial_send", f"session={session_id}", f"data={data[:40]!r}")

        sess = self._serial_sessions.get(session_id)
        if not sess:
            return (
                f"[error] session_id '{session_id}' not found.\n"
                f"Valid sessions: {list(self._serial_sessions.keys())}\n"
                "Call serial_open first."
            )

        port      = sess["port"]
        baudrate  = sess["baudrate"]
        s_timeout = sess["timeout"]

        if hex_encode:
            send_expr = f"bytes.fromhex('{data.replace(' ', '')}')"
        else:
            data_escaped = data.replace("'", "\\'")
            send_expr = f"'{data_escaped}'.encode().decode('unicode_escape').encode('latin1')"

        if expect:
            recv_code = (
                f"buf=b''; deadline=time.time()+{timeout}\n"
                f"    while time.time()<deadline:\n"
                f"        chunk=s.read(s.in_waiting or 1)\n"
                f"        if chunk: buf+=chunk\n"
                f"        if b{expect!r} in buf: break\n"
                f"        time.sleep(0.01)\n"
            )
        else:
            recv_code = (
                f"time.sleep({timeout})\n"
                f"    buf=s.read(s.in_waiting or 1)\n"
            )

        script = (
            f"import serial, time\n"
            f"s = serial.Serial('{port}', {baudrate}, timeout={s_timeout})\n"
            f"tx = {send_expr}\n"
            f"s.write(tx)\n"
            f"s.flush()\n"
            f"{recv_code}"
            f"s.close()\n"
            f"print('TX:', tx)\n"
            f"print('RX:', buf)\n"
            f"print('RX_TEXT:', buf.decode('utf-8', errors='replace'))\n"
        )

        result = self.conn.upload_and_run(script, interpreter="python3", timeout=int(timeout) + 5)
        _print_result(result)

        out = result.output()
        sess["history"].append({"tx": data, "rx": out, "hex": hex_encode})
        if len(sess["history"]) > 50:
            sess["history"].pop(0)

        return result.to_tool_result()

    # ─── serial_close ──────────────────────────────────────────

    def _serial_close(self, inp: dict) -> str:
        session_id = inp.get("session_id", "")

        if not session_id:
            n = len(self._serial_sessions)
            self._serial_sessions.clear()
            _print_tool("serial_close", "all", f"closing {n} sessions")
            return f"[serial_close] {n} sessions closed."

        _print_tool("serial_close", f"session={session_id}")

        if session_id not in self._serial_sessions:
            return f"[error] session_id '{session_id}' not found."

        sess = self._serial_sessions.pop(session_id)
        history_count = len(sess["history"])
        print(f"    Closed {sess['port']} (io count: {history_count})", flush=True)
        return (
            f"[serial_close ok] session_id={session_id} closed.\n"
            f"port={sess['port']} | total io={history_count}"
        )

    # ─── todo ──────────────────────────────────────────────────

    def _todo(self, inp: dict) -> str:
        todos = inp.get("todos", [])
        self.todos.update(todos)
        formatted = self.todos.format_display()
        print(f"\n{formatted}")
        return f"[ok] todo updated\n{self.todos.format_for_llm()}"

    # ─── remember ──────────────────────────────────────────────

    def _remember(self, inp: dict) -> str:
        ns    = inp.get("namespace", "hardware")
        key   = inp.get("key", "").strip()
        value = inp.get("value")

        if not key:
            return "[error] remember: key is required"

        if self.memory is None:
            return "[warn] remember: memory not initialized. Call ssh_connect first."

        self.memory.remember(ns, key, value)
        _print_tool("remember", f"[{ns}] {key} = {value}")
        return f"[ok] remembered: [{ns}] {key} = {value}"

    # ─── verify ────────────────────────────────────────────────

    def _verify(self, inp: dict) -> str:
        target = inp["target"]
        device = inp.get("device", "")

        _print_tool("verify", f"[{target}] {device}", "hardware verification")

        if target == "custom":
            if device:
                result = self.conn.run(device, timeout=30)
            else:
                return "[error] custom verify: put the bash command to run in the device field"
            _print_result(result)
            return result.to_tool_result()

        cmd_template = VERIFY_COMMANDS.get(target)
        if not cmd_template:
            return f"[error] Unknown verify target: {target}"

        cmd = f"export ECC_DEVICE='{device}'\n{cmd_template}"
        result = self.conn.run(cmd, timeout=60)
        _print_result(result)

        out = result.to_tool_result()
        summary = " | ".join(
            l.strip() for l in result.output().splitlines()
            if any(k in l for k in ("PASS", "FAIL", "WARN", "OK"))
        )[:200]
        return (f"[verify:{target} {device}] {summary}\n\n{out}") if summary else out

    # ─── subagent ──────────────────────────────────────────────

    def _subagent(self, inp: dict) -> str:
        return "[error] subagent must be handled by AgentLoop, not ToolExecutor"

    # ─── physical safety guard ─────────────────────────────────

    def _physical_safety_check(self, cmd: str) -> str:
        """
        Pre-execution physical constraint validation based on constraints memory.

        Returns "" if safe, or a reason string if blocked.
        """
        if self.memory is None:
            return ""
        constraints = self.memory.semantic.constraints
        if not constraints:
            return ""

        cmd_lower = cmd.lower()

        max_erpm = constraints.get("max_erpm")
        if max_erpm is not None:
            for m in __import__("re").finditer(r"data[\"']?\s*[:=]\s*(\d+(?:\.\d+)?)", cmd):
                try:
                    val = float(m.group(1))
                    if val > float(max_erpm):
                        return (
                            f"ERPM {val} > max_erpm {max_erpm} (constraints memory). "
                            f"Use a value <= {max_erpm} to prevent motor damage."
                        )
                except ValueError:
                    pass

        max_speed = constraints.get("max_speed_ms") or constraints.get("max_speed")
        if max_speed is not None:
            for m in __import__("re").finditer(
                r"(?:speed|linear\.x|velocity)\s*[\":]\s*(\d+(?:\.\d+)?)", cmd
            ):
                try:
                    val = float(m.group(1))
                    if val > float(max_speed):
                        return (
                            f"Speed {val} m/s > max_speed {max_speed} m/s (constraints memory). "
                            f"Use a value <= {max_speed} m/s."
                        )
                except ValueError:
                    pass

        max_temp = constraints.get("max_temp_c")
        if max_temp is not None:
            for m in __import__("re").finditer(r"temp\w*\s*[=:]\s*(\d+(?:\.\d+)?)", cmd_lower):
                try:
                    val = float(m.group(1))
                    if val > float(max_temp):
                        return (
                            f"Temperature {val}C > max_temp {max_temp}C (constraints memory)."
                        )
                except ValueError:
                    pass

        return ""

    # ─── ask_user ──────────────────────────────────────────────

    def _ask_user(self, inp: dict) -> str:
        question = inp["question"]
        context = inp.get("context", "")

        print("\n" + "─" * 60, flush=True)
        if context:
            print(f"  Info: {context}", flush=True)
        print(f"  Question: {question}", flush=True)
        print("─" * 60, flush=True)
        try:
            answer = input("  Answer: ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
            print("\n  (no input — treating as empty)", flush=True)

        if not answer:
            return "[ask_user] No answer provided. Proceed with best-effort assumption."
        return f"[ask_user] User answered: {answer}"

    # ─── done ──────────────────────────────────────────────────

    def _done(self, inp: dict) -> str:
        success = inp.get("success", False)
        summary = inp.get("summary", "")
        evidence = inp.get("evidence", "")
        notes = inp.get("notes", "")

        running = [tid for tid, t in self._bg_tasks.items() if not t.done]
        if running:
            print(
                f"\n  Warning: done() called with background tasks still running: {running}",
                flush=True
            )

        if self._serial_sessions:
            n = len(self._serial_sessions)
            print(f"\n  Auto-closing {n} open serial session(s)...", flush=True)
            self._serial_sessions.clear()

        icon = "OK" if success else "FAIL"
        print(f"\n{'='*60}")
        print(f"  [{icon}] {summary}")
        if evidence:
            print(f"  Evidence: {evidence}")
        if notes:
            print(f"  Notes: {notes}")
        print(f"{'='*60}")

        self.is_finished = True
        return "done"


def _local_run(cmd: str, timeout: int = 30) -> ExecResult:
    import time
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        return ExecResult(
            ok=r.returncode == 0,
            stdout=r.stdout, stderr=r.stderr,
            rc=r.returncode, duration_ms=elapsed,
        )
    except subprocess.TimeoutExpired:
        elapsed = int((time.monotonic() - t0) * 1000)
        return ExecResult(ok=False, stdout="", stderr=f"local timeout after {timeout}s", rc=-1, duration_ms=elapsed)
    except Exception as e:
        return ExecResult(ok=False, stdout="", stderr=str(e), rc=-1)


def _local_write(path: str, content: str, mode: str = "") -> ExecResult:
    import time
    t0 = time.monotonic()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if mode:
            os.chmod(path, int(mode, 8))
        elapsed = int((time.monotonic() - t0) * 1000)
        return ExecResult(ok=True, stdout=f"written {len(content)} bytes to {path}", stderr="", rc=0, duration_ms=elapsed)
    except Exception as e:
        return ExecResult(ok=False, stdout="", stderr=str(e), rc=1)


def _print_tool(name: str, detail: str = "", desc: str = ""):
    desc_str = f"  # {desc}" if desc else ""
    print(f"\n  > {name}  {detail}{desc_str}", flush=True)

def _print_result(result: ExecResult, max_chars: int = 4000):
    out = result.output()
    if not out.strip():
        return
    if len(out) > max_chars:
        head = out[:max_chars // 2]
        tail = out[-(max_chars // 4):]
        out = f"{head}\n  ...({len(out)} chars truncated)...\n{tail}"
    for line in out.splitlines():
        print(f"  {line}", flush=True)
