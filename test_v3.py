import sys
sys.path.insert(0, '/home/claude/repo')

# ── 1. parse_error_feedback ────────────────────────────────
from ecc_core.verifier import parse_error_feedback

cases = [
    ("package 'ackermann_msgs' not found", "missing_dep"),
    ("serial.SerialException: could not open port", "serial_error"),
    ("Permission denied /dev/ttyACM0", "permission"),
    ("timeout after 30s", "ssh_error"),
    ("ModuleNotFoundError: No module named 'serial'", "missing_dep"),
    ("QoS mismatch detected", "ros2_error"),
    ("[ok] 127ms\nsome output", None),  # 정상 → None
]
for text, expected_type in cases:
    fb = parse_error_feedback(text)
    if expected_type is None:
        assert fb is None, f"정상 텍스트인데 feedback 반환: {fb}"
    else:
        assert fb is not None, f"feedback 없음: {text[:40]}"
        assert fb["error_type"] == expected_type, f"타입 불일치: {fb['error_type']} != {expected_type}"
print("✅ 1. parse_error_feedback 분류")

# ── 2. verify_execution feedback 필드 ─────────────────────
from ecc_core.verifier import verify_execution

obs_fail = {"ok": False, "stdout": "", "stderr": "", "response": "",
            "raw": "[error] serial.SerialException: could not open port /dev/ttyACM0"}
r = verify_execution("bash", obs_fail)
assert r["success"] == False
assert r["feedback"] is not None
assert r["feedback"]["error_type"] == "serial_error"
assert "suggested_fix" in r["feedback"]
print("✅ 2. verify_execution feedback 포함")

# ── 3. verify_motion feedback ─────────────────────────────
from ecc_core.verifier import verify_motion

r_fail = verify_motion("speed: 0.0, no data from topic")
assert r_fail["success"] == False
assert r_fail["feedback"]["error_type"] == "hardware_fault"
r_ok = verify_motion("current speed=1.5 m/s, erpm=2500")
assert r_ok["success"] == True
assert r_ok["feedback"] is None
print("✅ 3. verify_motion feedback")

# ── 4. probe_registry 플러그인 레지스트리 ─────────────────
from ecc_core.tools import probe_registry, verify_registry

assert "hw" in probe_registry.list_targets()
assert "all" in probe_registry.list_targets()
assert "serial_device" in verify_registry.list_targets()

# 런타임 등록
probe_registry.register("stm32", "st-info --probe 2>/dev/null || echo '(st-info 없음)'")
assert "stm32" in probe_registry.list_targets()
assert "st-info" in probe_registry.get("stm32")

# 중복 등록 방지
try:
    probe_registry.register("stm32", "duplicate")
    assert False, "중복 등록 허용됨"
except ValueError:
    pass  # 정상
probe_registry.register("stm32", "overwritten", overwrite=True)
assert "overwritten" in probe_registry.get("stm32")
print("✅ 4. probe_registry 플러그인 레지스트리")

# ── 5. SubagentRole 도구 세트 분리 ────────────────────────
from ecc_core.dispatcher import SubagentRole, _subagent_config
from ecc_core.connection import BoardConnection

# 테스트용 mock conn
class MockConn:
    user = "ubuntu"; host = "192.168.1.100"; port = 22

conn = MockConn()

sys_e, tools_e = _subagent_config(SubagentRole.EXPLORER, conn, "")
sys_s, tools_s = _subagent_config(SubagentRole.SETUP,    conn, "")
sys_v, tools_v = _subagent_config(SubagentRole.VERIFIER, conn, "")

tool_names_e = {t["name"] for t in tools_e}
tool_names_s = {t["name"] for t in tools_s}
tool_names_v = {t["name"] for t in tools_v}

# EXPLORER: write 없음, report 있음
assert "write"  not in tool_names_e, "EXPLORER에 write 있음"
assert "report" in tool_names_e

# SETUP: write 있음, subagent/done 없음
assert "write"    in tool_names_s, "SETUP에 write 없음"
assert "subagent" not in tool_names_s
assert "done"     not in tool_names_s

# VERIFIER: bash만 있고 write/script 없음
assert "bash"   in tool_names_v
assert "write"  not in tool_names_v
assert "script" not in tool_names_v
assert "report" in tool_names_v

# system prompt 내용 확인
assert "SETUP" in sys_s
assert "VERIFIER" in sys_v
assert "EXPLORER" in sys_e
print("✅ 5. SubagentRole 도구 세트 + system prompt 분리")

# ── 6. executor 물리 안전 guard ───────────────────────────
from ecc_core.executor import ToolExecutor
from ecc_core.todo import TodoManager
from ecc_core.memory import ECCMemory

mem = ECCMemory()
mem.remember("constraints", "max_erpm", 3000)
mem.remember("constraints", "max_speed_ms", 1.5)

todos = TodoManager()
ex = ToolExecutor(conn=None, todos=todos, memory=mem)

# ERPM 초과
result = ex._physical_safety_check('ros2 topic pub /cmd data: 5000')
assert "safety" in result.lower() or "erpm" in result.lower() or "5000" in result, f"ERPM guard 미작동: {result}"

# 안전한 명령
safe = ex._physical_safety_check('echo hello')
assert safe == "", f"안전한 명령 차단됨: {safe}"
print("✅ 6. 물리 안전 guard (constraints memory 기반)")

print()
print("━" * 50)
print("v3 전체 검증 완료 (6/6 pass) ✅")
