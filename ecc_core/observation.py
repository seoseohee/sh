"""
ecc_core/observation.py

Observation Collector — tool_result를 공통 스키마로 정규화.

지금까지 observation이 tool_result / verify stdout / serial 응답 등으로
흩어져 있던 것을 collect_observation() 하나로 통일.

반환 스키마:
  {
    "tool":     도구 이름,
    "stdout":   표준 출력,
    "stderr":   표준 오류,
    "response": serial / HTTP 등 응답,
    "ok":       성공 여부,
    "raw":      원본 result 문자열,
  }

verifier.py가 이 스키마만 보면 되도록 설계.
"""

from __future__ import annotations


def collect_observation(tool_name: str, result_text: str) -> dict:
    """
    executor.execute() 반환 문자열을 공통 관찰 스키마로 변환.

    result_text: to_tool_result() 또는 executor._xxx() 반환값.
    """
    text = result_text or ""
    ok = not (
        text.startswith("[error]")
        or text.startswith("[blocked]")
        or text.startswith("[can_execute blocked]")
        or "rc=-1" in text
    )

    # stdout / stderr 분리 (ExecResult.output() 포맷 기준)
    stdout, stderr = text, ""
    if "[stderr]" in text:
        parts = text.split("[stderr]", 1)
        stdout = parts[0].strip()
        stderr = parts[1].strip() if len(parts) > 1 else ""

    # serial response 추출 (RX_TEXT: 패턴)
    response = ""
    for line in text.splitlines():
        if line.startswith("RX_TEXT:"):
            response = line[len("RX_TEXT:"):].strip()
            break

    return {
        "tool":     tool_name,
        "stdout":   stdout[:600],
        "stderr":   stderr[:200],
        "response": response[:200],
        "ok":       ok,
        "raw":      text[:800],
    }
