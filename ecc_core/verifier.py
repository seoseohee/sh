"""
ecc_core/verifier.py

v3 변경 (EmbedAgent ICSE 2026 기반):
  - parse_error_feedback(): 에러 텍스트 → 구조화 피드백 변환
    EmbedAgent 논문의 "compiler feedback loop" 패턴을 임베디드 도메인에 적용.
  - verify_execution(): tool_name 기반 판정 (v2 유지)
  - verify_motion(): 물리 동작 특화 검증 + feedback 필드 추가
"""

from __future__ import annotations
import re

VERIFY_TOOL_TARGETS = {
    "serial_device", "i2c_device", "network_device",
    "ros2_topic", "process", "system", "custom",
}

_MOTION_OK   = ("moved", "distance", "velocity", "odom", "erpm", "speed")
_MOTION_FAIL = ("not moving", "speed: 0.0", "speed=0.0", "no data")
_ERROR_KW    = ("error", "failed", "exception", "traceback", "rc=-1")

# (pattern, error_type, root_cause, suggested_fix, retry_safe)
_ERROR_RULES: list[tuple[str, str, str, str, bool]] = [
    (r"package '(.+?)' not found",
     "missing_dep", "ROS2 패키지 미설치",
     "sudo apt-get install -y ros-$ROS_DISTRO-{pkg} 실행", False),
    (r"No executable found|ExecutableNotFound",
     "missing_dep", "ROS2 실행 파일 없음",
     "패키지 설치 및 source setup.bash 확인", False),
    (r"QoS mismatch|incompatible QoS",
     "ros2_error", "ROS2 QoS 불일치",
     "publisher/subscriber QoS 맞추거나 --qos-reliability best_effort 사용", False),
    (r"no new message|no data",
     "ros2_error", "ROS2 토픽 데이터 없음 — publisher 없거나 QoS 불일치",
     "ros2 topic info로 publisher 수 확인, QoS 확인", False),
    (r"Failed to import|ModuleNotFoundError|ImportError",
     "missing_dep", "Python 패키지 미설치",
     "pip3 install {module} --break-system-packages", False),
    (r"serial\.SerialException|could not open port|No such file or directory.*tty",
     "serial_error", "시리얼 포트 없음 또는 권한 없음",
     "ls /dev/tty* 로 포트 확인, sudo chmod a+rw /dev/ttyACM0", False),
    (r"Permission denied.*tty|permission denied.*serial",
     "permission", "시리얼 포트 접근 권한 없음",
     "sudo chmod a+rw /dev/ttyACM0 또는 usermod -a -G dialout $USER", False),
    (r"Permission denied|Operation not permitted",
     "permission", "파일/디바이스 접근 권한 없음",
     "sudo 실행 또는 chmod/chown으로 권한 부여", False),
    (r"fault_code|FAULT|overcurrent|overvoltage|overtemp",
     "hardware_fault", "하드웨어 fault 감지",
     "전원/배선 확인, fault 코드 조회 후 reset 필요", False),
    (r"timeout after|timed out",
     "ssh_error", "SSH 또는 명령 타임아웃",
     "timeout 값 증가 또는 보드 연결 상태 확인", True),
    (r"Connection reset|Broken pipe|ssh.*closed",
     "ssh_error", "SSH 연결 끊김",
     "ssh_connect()로 재연결 후 재시도", True),
    (r"command not found|No such file.*bin|not installed",
     "missing_dep", "명령 또는 패키지 미설치",
     "apt-get install 또는 pip3 install로 설치", False),
]


def parse_error_feedback(result_text: str) -> "dict | None":
    """
    tool_result 텍스트에서 구조화 에러 피드백 추출.

    EmbedAgent 논문의 compiler feedback loop:
    에러를 그대로 LLM에 넘기는 대신 유형/원인/수정 힌트로 구조화해서
    LLM이 불필요한 탐색 없이 바로 수정 행동을 취하게 함.
    """
    if not result_text:
        return None
    text_lower = result_text.lower()
    is_error = (
        result_text.startswith("[error]")
        or "rc=-1" in result_text
        or any(kw in text_lower for kw in (
            "error", "failed", "exception", "traceback",
            "not found", "permission denied", "no such file",
            "timeout", "connection reset", "fault",
            "mismatch", "no module", "no data",
        ))
    )
    if not is_error:
        return None

    for pattern, error_type, root_cause, suggested_fix, retry_safe in _ERROR_RULES:
        m = re.search(pattern, result_text, re.IGNORECASE)
        if m:
            if m.lastindex and "{" in suggested_fix:
                try:
                    suggested_fix = suggested_fix.format(pkg=m.group(1), module=m.group(1))
                except (IndexError, KeyError):
                    pass
            return {"error_type": error_type, "root_cause": root_cause,
                    "suggested_fix": suggested_fix, "retry_safe": retry_safe}

    return {"error_type": "generic", "root_cause": "알 수 없는 오류",
            "suggested_fix": "에러 메시지를 확인하고 원인을 분석하세요", "retry_safe": False}


def verify_execution(tool_name: str, observation: dict) -> dict:
    raw = observation.get("raw", "")

    if not observation.get("ok", True):
        return {"success": False, "reason": "execution_error",
                "evidence": raw[:300], "feedback": parse_error_feedback(raw)}

    combined = " ".join([
        observation.get("stdout", ""),
        observation.get("stderr", ""),
        observation.get("response", ""),
    ]).lower()

    if any(kw in combined for kw in _ERROR_KW):
        return {"success": False, "reason": "execution_error",
                "evidence": combined[:300], "feedback": parse_error_feedback(raw)}

    is_verify_tool = (tool_name == "verify")
    if is_verify_tool:
        has_pass = "pass" in combined or any(kw in combined for kw in _MOTION_OK)
        has_fail = "fail" in combined or "warn" in combined
        if has_pass and not has_fail:
            return {"success": True,  "reason": "verified",          "evidence": combined[:300], "feedback": None}
        if has_fail:
            return {"success": False, "reason": "verification_weak", "evidence": combined[:300], "feedback": parse_error_feedback(raw)}
        if combined.strip():
            return {"success": False, "reason": "verification_weak", "evidence": combined[:300], "feedback": None}
        return {"success": False, "reason": "no_observable_output",  "evidence": "",             "feedback": None}

    if not combined.strip():
        return {"success": False, "reason": "no_observable_output", "evidence": "", "feedback": None}

    return {"success": True, "reason": "observable_output_present", "evidence": combined[:300], "feedback": None}


def verify_motion(evidence: str) -> dict:
    text = evidence.lower()
    fail = any(kw in text for kw in _MOTION_FAIL)
    ok   = any(kw in text for kw in _MOTION_OK) and not fail
    return {
        "success":  ok,
        "reason":   "motion_verified" if ok else "motion_not_verified",
        "evidence": evidence[:300],
        "feedback": None if ok else {
            "error_type":    "hardware_fault",
            "root_cause":    "물리 동작 미확인 — 모터 deadband 또는 명령 미전달",
            "suggested_fix": "telemetry 확인, ERPM/속도 임계값 점검, constraints memory 확인",
            "retry_safe":    False,
        },
    }
