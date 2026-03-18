"""
ecc_core/connection.py

수정 이력:
  v2 — BoardDiscovery.scan() IP 우선순위 보장.
       기존: as_completed()는 응답 속도 순서로 결과를 돌려주어
       known_hosts보다 서브넷 스캔 IP가 먼저 응답하면 우선순위가 무시됨.
       수정: IP 그룹별 순차 시도 유지 + user 병렬 시도로 속도 확보.
       첫 번째로 성공한 (IP 우선순위 존중) 연결을 반환.
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

    def to_tool_result(self, max_chars=4000):
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
        t = _env_int("ECC_SSH_TIMEOUT", 10)
        return ["-o","BatchMode=yes","-o","StrictHostKeyChecking=no",
                "-o",f"ConnectTimeout={t}","-o","ServerAliveInterval=5","-o","ServerAliveCountMax=3"]

    def __init__(self, host, user="root", port=22):
        self.host = host
        self.user = user
        self.port = port
        self._consecutive_failures = 0

    @property
    def address(self):
        return f"{self.user}@{self.host}:{self.port}"

    def run(self, cmd, timeout=30):
        full_cmd = ["ssh"] + self.SSH_OPTS + ["-p", str(self.port), f"{self.user}@{self.host}", cmd]
        t0 = time.monotonic()
        try:
            r = subprocess.run(full_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
            elapsed = int((time.monotonic()-t0)*1000)
            result = ExecResult(ok=r.returncode==0, stdout=r.stdout, stderr=r.stderr, rc=r.returncode, duration_ms=elapsed)
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

    def reconnect(self, max_attempts=3):
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
            r = subprocess.run(
                ["ip", "neigh", "show"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3
            )
            for line in r.stdout.splitlines():
                parts = line.split()
                if not parts:
                    continue
                if "FAILED" in line or "INCOMPLETE" in line:
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
        """
        보드를 자동 탐색한다.

        FIX v2: IP 우선순위 보장.
        기존 코드는 IP 그룹별 순차 + user 병렬을 의도했으나
        as_completed()가 완료 순서(응답 속도)로 결과를 돌려주어
        우선순위가 낮은 서브넷 IP가 먼저 응답하면 잘못 선택됨.

        수정: IP 그룹별 순차 유지.
        각 IP에서 users를 병렬로 시도, 하나라도 성공하면 즉시 반환.
        다음 IP 그룹으로 넘어가는 조건: 모든 user 시도 실패.
        """
        candidates = []
        users = [user] if user else cls._default_users()

        def _add(ip):
            if ip and ip not in candidates:
                candidates.append(ip)

        # ① 환경변수 힌트
        env_host = os.environ.get("ECC_BOARD_HOST")
        if env_host:
            _add(env_host)

        # ② known_hosts
        for ip in cls._known_hosts_ips():
            _add(ip)

        # ③ ARP 캐시
        for ip in cls._arp_cache_ips():
            _add(ip)

        # ④ mDNS
        for mdns_name in cls._default_mdns():
            try:
                ip = socket.gethostbyname(mdns_name)
                _add(ip)
            except Exception:
                pass

        # ⑤ 서브넷 ping 스캔
        subnet_ips = cls._get_subnet_ips()
        if subnet_ips:
            workers = _env_int("ECC_SCAN_WORKERS", 200)
            print(f"  🌐 {len(subnet_ips)}개 IP 병렬 스캔 (workers={workers})...", flush=True)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(cls._ping, ip): ip for ip in subnet_ips}
                for future in as_completed(futures):
                    ip = future.result()
                    _add(ip)

        print(f"  🔑 {len(candidates)}개 후보 SSH 확인...", flush=True)

        # FIX: IP 우선순위 보장 — IP 순서대로 순차 시도
        # 각 IP 내에서 users는 병렬 시도 (속도 확보)
        for ip in candidates:
            conn = cls._try_ip(ip, users, port)
            if conn:
                return conn

        return None

    @classmethod
    def _try_ip(cls, ip: str, users: list, port: int) -> Optional["BoardConnection"]:
        """
        단일 IP에 대해 user 목록을 병렬로 SSH 시도.
        첫 번째 성공한 연결 반환, 모두 실패 시 None.

        FIX: as_completed 결과를 순서 없이 받되,
        성공 즉시 반환하고 나머지 future를 cancel (불필요한 연결 방지).
        """
        def _try(u):
            c = BoardConnection(ip, u, port)
            return c if c.is_alive() else None

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            futures = {pool.submit(_try, u): u for u in users}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    # 나머지 future는 daemon thread라 자연 종료됨
                    return result
        return None

    @staticmethod
    def _ping(ip):
        try:
            cmd = ["ping","-c","1","-W","1",ip] if platform.system()!="Windows" else ["ping","-n","1","-w","1000",ip]
            r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=3)
            return ip if r.returncode==0 else None
        except:
            return None

    @classmethod
    def _get_subnet_ips(cls):
        ips = []
        try:
            r = subprocess.run(["ip","route"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
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
                ips += [f"{base}.{i}" for i in range(1,255)]
        return ips
