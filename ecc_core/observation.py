"""
ecc_core/observation.py

Observation Collector — normalizes tool_result to a common schema.

Previously observations were scattered across tool_result / verify stdout / serial responses etc.
Now unified under collect_observation().

Return schema:
  {
    "tool":     tool name,
    "stdout":   standard output,
    "stderr":   standard error,
    "response": serial / HTTP response,
    "ok":       success flag,
    "raw":      raw result string,
  }

Designed so verifier.py only needs to see this schema.
"""

from __future__ import annotations


def collect_observation(tool_name: str, result_text: str) -> dict:
    """
    Convert executor.execute() return string to common observation schema.

    result_text: return value from to_tool_result() or executor._xxx().
    """
    text = result_text or ""
    ok = not (
        text.startswith("[error]")
        or text.startswith("[blocked]")
        or text.startswith("[can_execute blocked]")
        or "rc=-1" in text
    )

    # separate stdout / stderr (based on ExecResult.output() format)
    stdout, stderr = text, ""
    if "[stderr]" in text:
        parts = text.split("[stderr]", 1)
        stdout = parts[0].strip()
        stderr = parts[1].strip() if len(parts) > 1 else ""

    # extract serial response (RX_TEXT: pattern)
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
