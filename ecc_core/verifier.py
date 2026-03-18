"""
ecc_core/verifier.py

Execution Verifier — observation 스키마를 받아 실행 성공/실패를 판정.

verify_execution() : 일반 tool_result 검증
verify_motion()    : 물리 동작(모터/이동) 특화 검증

수정 이력:
  v2 — verify 분기 dead code 수정.
       기존: action 파라미터에 "verify" 문자열 포함 여부를 검사했으나
       loop.py가 tool name("ros2_topic", "serial_device" 등)을 넘기기 때문에
       "verify"가 포함되는 경우가 없어 해당 분기가 항상 건너뛰어짐.
       수정: is_verify_tool 파라미터를 명시적으로 받아 판정.

반환 스키마:
  {
    "success":  bool,
    "reason":   str,
    "evidence": str,
  }

reason 값:
  "execution_error"           — stderr / exit code 오류
  "no_observable_output"      — 빈 출력
  "verification_weak"         — verify 도구가 기대 키워드를 못 찾음
  "motion_not_verified"       — 속도/거리/odom 없음
  "observable_output_present" — 정상
  "motion_verified"           — 물리 동작 확인
"""

from __future__ import annotations

# verify 도구 target 목록 — loop.py의 VERIFY_COMMANDS 키와 일치해야 함
VERIFY_TOOL_TARGETS = {
    "serial_device", "i2c_device", "network_device",
    "ros2_topic", "process", "system", "custom",
}

# 물리 동작 성공 키워드
_MOTION_OK = ("moved", "distance", "velocity", "odom", "erpm", "speed")
_MOTION_FAIL = ("not moving", "speed: 0.0", "speed=0.0", "no data")

# 일반 오류 키워드
_ERROR_KW = ("error", "failed", "exception", "traceback", "rc=-1")


def verify_execution(tool_name: str, observation: dict) -> dict:
    """
    tool_result를 검증.

    tool_name: 에이전트가 실행한 도구 이름 (예: "verify", "bash", "script")
    observation: collect_observation() 반환값

    FIX v2: is_verify_tool을 tool_name == "verify" 로 판정.
    기존에는 action 문자열에 "verify"가 포함되는지 검사했는데,
    loop.py가 넘기는 값은 "ros2_topic", "serial_device" 등 target 이름이라
    이 분기가 항상 False였음.
    """
    # 실행 자체 실패
    if not observation.get("ok", True):
        text = observation.get("raw", "")
        return {
            "success": False,
            "reason": "execution_error",
            "evidence": text[:300],
        }

    combined = " ".join([
        observation.get("stdout", ""),
        observation.get("stderr", ""),
        observation.get("response", ""),
    ]).lower()

    # 오류 키워드 감지
    if any(kw in combined for kw in _ERROR_KW):
        return {
            "success": False,
            "reason": "execution_error",
            "evidence": combined[:300],
        }

    # FIX: verify 도구 특화 판정 — tool_name으로 명확하게 판별
    is_verify_tool = (tool_name == "verify")
    if is_verify_tool:
        has_pass = "pass" in combined or any(kw in combined for kw in _MOTION_OK)
        has_fail = "fail" in combined or "warn" in combined
        if has_pass and not has_fail:
            return {"success": True,  "reason": "verified",          "evidence": combined[:300]}
        if has_fail:
            return {"success": False, "reason": "verification_weak", "evidence": combined[:300]}
        # PASS/FAIL 키워드 없음 — 출력 있으면 weak 판정
        if combined.strip():
            return {"success": False, "reason": "verification_weak", "evidence": combined[:300]}
        return {"success": False, "reason": "no_observable_output", "evidence": ""}

    # 출력 없음
    if not combined.strip():
        return {
            "success": False,
            "reason": "no_observable_output",
            "evidence": "",
        }

    return {
        "success": True,
        "reason": "observable_output_present",
        "evidence": combined[:300],
    }


def verify_motion(evidence: str) -> dict:
    """
    물리 동작(모터/이동) 특화 검증.
    """
    text = evidence.lower()
    fail = any(kw in text for kw in _MOTION_FAIL)
    ok   = any(kw in text for kw in _MOTION_OK) and not fail
    return {
        "success": ok,
        "reason":  "motion_verified" if ok else "motion_not_verified",
        "evidence": evidence[:300],
    }
