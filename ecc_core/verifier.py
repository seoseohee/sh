"""
ecc_core/verifier.py

Execution Verifier — observation 스키마를 받아 실행 성공/실패를 판정.

verify_execution() : 일반 tool_result 검증
verify_motion()    : 물리 동작(모터/이동) 특화 검증

반환 스키마:
  {
    "success":  bool,
    "reason":   str,   ← recovery.py / reflection.py의 분기 키
    "evidence": str,
  }

reason 값 목록 (FAILURE_ROUTING 키와 대응):
  "execution_error"        — stderr / exit code 오류
  "no_observable_output"   — 빈 출력 (타임아웃 등)
  "verification_weak"      — verify 도구가 기대 키워드를 못 찾음
  "motion_not_verified"    — 속도/거리/odom 없음
  "observable_output_present" — 정상
  "motion_verified"        — 물리 동작 확인
"""

from __future__ import annotations


# 물리 동작 성공 키워드 (odom, ERPM, velocity 등)
# "ok" / "success" 제외 — "[ok] 127ms", "[success] Connected" 등 비동작 메시지에서 오탐 유발
_MOTION_OK = (
    "moved", "distance", "velocity", "odom",
    "erpm", "speed",
)
_MOTION_FAIL = ("not moving", "speed: 0.0", "speed=0.0", "no data")

# 일반 오류 키워드
_ERROR_KW = ("error", "failed", "exception", "traceback", "rc=-1")


def verify_execution(action: str, observation: dict) -> dict:
    """
    일반 tool_result를 검증.

    action: 에이전트가 실행한 행동 설명 (예: "publish /cmd_vel")
    observation: collect_observation() 반환값
    """
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

    # verify 계열 도구는 기대 키워드 필요
    if "verify" in action.lower():
        if any(kw in combined for kw in _MOTION_OK):
            return {"success": True,  "reason": "verified",          "evidence": combined[:300]}
        return     {"success": False, "reason": "verification_weak", "evidence": combined[:300]}

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
    verify(target='ros2_topic') 결과 등을 evidence로 받아 판정.
    """
    text = evidence.lower()
    fail = any(kw in text for kw in _MOTION_FAIL)
    ok   = any(kw in text for kw in _MOTION_OK) and not fail
    return {
        "success": ok,
        "reason":  "motion_verified" if ok else "motion_not_verified",
        "evidence": evidence[:300],
    }
