"""
ecc_core/connection.py

Changelog:
  v2 — Ensure IP priority in BoardDiscovery.scan().
  v3 — [env overrides] SSH 하드코딩 파라미터 환경변수 오버라이드 추가.
         ECC_SSH_RECONNECT_ATTEMPTS : SSH 재연결 시도 횟수 (기본 3)
         ECC_SSH_KEEPALIVE_INTERVAL : SSH ServerAliveInterval 초 (기본 5)
         ECC_SSH_KEEPALIVE_COUNT    : SSH ServerAliveCountMax (기본 3)
         ECC_PING_TIMEOUT           : ping 단일 대기 시간 ms (기본 1000)
         ECC_TOOL_OUTPUT_MAX_CHARS  : 툴 결과 출력 상한 문자수 (기본 4000)
"""

import platform
import subprocess
import time
import os
import ipaddress
import socket
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass


def _env_list(key, default):
    return [v.strip() for v in os.environ.get(key, default).split(",") if v.strip()]

def _env_int(key, default):
    try:
        return int(os.environ.get(key, default))
    except:
        return default


@dataclass
class ExecResult:
    ok: bool
    stdout: str
    stderr: str
    rc: int
    duration_ms: int = 0

    def output(self):
        parts = []
        if self.stdout.strip(): parts.append(self.stdout)
        if self.stderr.strip(): parts.append(f"[stderr]\n{self.stderr}")
        return "\n".join(parts) if parts else f"(no output, rc={self.rc})"

    def filtered_output(self):
        out = self.output()
        lines = out.splitlines()
        pub_lines = [l for l in lines if l.strip().startswith("publishing #") or l.strip().startswith("publisher:")]
        other_lines = [l for l in lines if not (l.strip().startswith("publishing #") or l.strip().startswith("publisher:"))]
        if len(pub_lines) > 2:
            return "\n".join(other_lines + [pub_lines[0], f"... [{len(pub_lines)} publish messages total] ..."])
        return out

    def to_tool_result(self, max_chars=None):
        # [env override] ECC_TOOL_OUTPUT_MAX_CHARS (기본 4000)
        if max_chars is None:
            max_chars = _env_int("ECC_TOOL_OUTPUT_MAX_CHARS", 4000)
        status = "ok" if self.ok else f"error(rc={self.rc})"
        out = self.filtered_output()
        if len(out) > max_chars:
            head = out[:max_chars//2]
            tail = out[-(max_chars//4):]
            out = f"{head}\n...[truncated]...\n{tail}"
        return f"[{status}] {self.duration_ms}ms\n{out}"


class BoardConnection:
    @property
    def SSH_OPTS(self):
        t         = _env_int("ECC_SSH_TIMEOUT", 10)
        # [env override] SSH keepalive 파라미터
        keepalive = _env_int("ECC_SSH_KEEPALIVE_INTERVAL", 5)
        keepcount = _env_int("ECC_SSH_KEEPALIVE_COUNT", 3)
        return [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"ConnectTimeout={t}",
            "-o", f"ServerAliveInterval={keepalive}",
            "-o", f"ServerAliveCountMax={keepcount}",
        ]

    def __init__(self, host, user=None, port=22):
        self.host = host
        self.user = user or os.environ.get("ECC_BOARD_USER", "root")
        self.port = port
        self._consecutive_failures = 0

    @property
    def address(self):
        return f"{self.user}@{self.host}:{self.port}"

    def run(self, cmd, timeout=30):
        full_cmd = ["ssh"] + self.SSH_OPTS + ["-p", str(self.port), f"{self.user}@{self.host}", cmd]
        t0 = time.monotonic()
        try:
            r = subprocess.run(full_cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            elapsed = int((time.monotonic()-t0)*1000)
            result = ExecResult(ok=r.returncode==0, stdout=r.stdout, stderr=r.stderr,
                                rc=r.returncode, duration_ms=elapsed)
            self._consecutive_failures = 0 if result.ok else self._consecutive_failures+1
            return result
        except subprocess.TimeoutExpired:
            elapsed = int((time.monotonic()-t0)*1000)
            self._consecutive_failures += 1
            return ExecResult(ok=False, stdout="", stderr=f"timeout after {timeout}s", rc=-1, duration_ms=elapsed)
        except Exception as e:
            self._consecutive_failures += 1
            return ExecResult(ok=False, stdout="", stderr=str(e), rc=-1)

    def upload_and_run(self, script, interpreter="bash", timeout=60):
        import base64 as _b64
        ts = int(time.time()*1000)
        remote_path = f"/tmp/_ecc_{ts}"
        b64 = _b64.b64encode(script.encode("utf-8")).decode("ascii")
        CHUNK = 4000
        chunks = [b64[i:i+CHUNK] for i in range(0, len(b64), CHUNK)]
        write_timeout = max(30, len(chunks)*5)
        if len(chunks)==1:
            r = self.run(f"printf '%s' {chunks[0]} | base64 -d > {remote_path}", timeout=write_timeout)
        else:
            lines = []
            for i, c in enumerate(chunks):
                op = ">" if i==0 else ">>"
                lines.append(f"printf '%s' {c} | base64 -d {op} {remote_path}.b64")
            lines.append(f"base64 -d {remote_path}.b64 > {remote_path} && rm -f {remote_path}.b64")
            r = self.run(" && ".join(lines), timeout=write_timeout)
        if not r.ok:
            return ExecResult(ok=False, stdout="", stderr=f"script write failed: {r.stderr}", rc=-1)
        return self.run(f"{interpreter} {remote_path}; _rc=$?; rm -f {remote_path}; exit $_rc", timeout=timeout)

    def is_alive(self):
        r = self.run("echo __ecc_ping__", timeout=6)
        return r.ok and "__ecc_ping__" in r.stdout

    def reconnect(self, max_attempts=None):
        # [env override] ECC_SSH_RECONNECT_ATTEMPTS (기본 3)
        if max_attempts is None:
            max_attempts = _env_int("ECC_SSH_RECONNECT_ATTEMPTS", 3)
        for attempt in range(max_attempts):
            time.sleep(2**attempt)
            if self.is_alive():
                self._consecutive_failures = 0
                return True
        return False

    @property
    def likely_disconnected(self):
        return self._consecutive_failures >= 3


class BoardDiscovery:
    @classmethod
    def _default_users(cls):
        return _env_list("ECC_USERS", "ubuntu,jetson,pi,root,admin,debian,user")

    @classmethod
    def _default_mdns(cls):
        return _env_list("ECC_MDNS", "jetson.local,raspberrypi.local,rpi.local,ubuntu.local,embedded.local,board.local")

    @classmethod
    def _default_subnets(cls):
        return _env_list("ECC_SUBNETS", "192.168.1,192.168.0,10.0.0,10.42.0")

    @classmethod
    def from_hint(cls, host, user, port):
        users = [user] if user else cls._default_users()
        def _try(u):
            c = BoardConnection(host, u, port)
            return c if c.is_alive() else None
        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            futures = {pool.submit(_try, u): u for u in users}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    return result
        return None

    @classmethod
    def _known_hosts_ips(cls) -> list:
        ips = []
        path = os.path.expanduser("~/.ssh/known_hosts")
        if not os.path.exists(path):
            return ips
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("|"):
                        continue
                    h = line.split()[0].split(",")[0]
                    if h.startswith("["):
                        h = h[1:].split("]")[0]
                    try:
                        ipaddress.ip_address(h)
                        if h not in ips:
                            ips.append(h)
                    except ValueError:
                        pass
        except Exception:
            pass
        return ips

    @classmethod
    def _arp_cache_ips(cls) -> list:
        ips = []
        try:
            r = subprocess.run(["ip", "neigh", "show"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=3)
            for line in r.stdout.splitlines():
                parts = line.split()
                if not parts or "FAILED" in line or "INCOMPLETE" in line:
                    continue
                try:
                    ipaddress.ip_address(parts[0])
                    if parts[0] not in ips:
                        ips.append(parts[0])
                except ValueError:
                    pass
        except Exception:
            pass
        return ips

    @classmethod
    def scan(cls, user=None, port=22):
        """Auto-discover the board with IP priority (v2 fix)."""
        candidates = []
        users = [user] if user else cls._default_users()

        def _add(ip):
            if ip and ip not in candidates:
                candidates.append(ip)

        env_host = os.environ.get("ECC_BOARD_HOST")
        if env_host:
            _add(env_host)

        for ip in cls._known_hosts_ips():
            _add(ip)
        for ip in cls._arp_cache_ips():
            _add(ip)

        for mdns_name in cls._default_mdns():
            try:
                ip = socket.gethostbyname(mdns_name)
                _add(ip)
            except Exception:
                pass

        subnet_ips = cls._get_subnet_ips()
        if subnet_ips:
            workers = _env_int("ECC_SCAN_WORKERS", 200)
            print(f"  🌐 {len(subnet_ips)} IPs parallel scan (workers={workers})...", flush=True)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(cls._ping, ip): ip for ip in subnet_ips}
                for future in as_completed(futures):
                    ip = future.result()
                    _add(ip)

        print(f"  🔑 {len(candidates)} candidates to try via SSH...", flush=True)

        for ip in candidates:
            conn = cls._try_ip(ip, users, port)
            if conn:
                return conn
        return None

    @classmethod
    def _try_ip(cls, ip: str, users: list, port: int) -> Optional["BoardConnection"]:
        def _try(u):
            c = BoardConnection(ip, u, port)
            return c if c.is_alive() else None
        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            futures = {pool.submit(_try, u): u for u in users}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    return result
        return None

    @staticmethod
    def _ping(ip):
        try:
            # [env override] ECC_PING_TIMEOUT ms 단위 (기본 1000ms = 1초)
            ping_wait_ms = _env_int("ECC_PING_TIMEOUT", 1000)
            if platform.system() != "Windows":
                wait_sec = max(1, ping_wait_ms // 1000)
                cmd = ["ping", "-c", "1", "-W", str(wait_sec), ip]
            else:
                cmd = ["ping", "-n", "1", "-w", str(ping_wait_ms), ip]
            r = subprocess.run(cmd, capture_output=True,
                               encoding="utf-8", errors="replace", timeout=3)
            return ip if r.returncode == 0 else None
        except:
            return None

    @classmethod
    def _get_subnet_ips(cls):
        ips = []
        try:
            r = subprocess.run(["ip", "route"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=5)
            for line in r.stdout.splitlines():
                parts = line.split()
                if parts and "/" in parts[0] and "via" not in line:
                    try:
                        net = ipaddress.ip_network(parts[0], strict=False)
                        if 16 <= net.prefixlen <= 24:
                            base = str(net.network_address).rsplit(".", 1)[0]
                            ips += [f"{base}.{i}" for i in range(1, 255)]
                    except:
                        pass
        except:
            pass
        if not ips:
            for base in cls._default_subnets():
                ips += [f"{base}.{i}" for i in range(1, 255)]
        return ips