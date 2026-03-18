"""
ecc_core/verifier.py

v3 changes (based on EmbedAgent ICSE 2026):
  - parse_error_feedback(): error text → structured feedback
    Applies EmbedAgent paper's 'compiler feedback loop' pattern to embedded domain.
  - verify_execution(): tool_name-based judgment (v2 retained)
  - verify_motion(): physical motion verification + feedback field
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
     "missing_dep", "ROS2 package not installed",
     "run sudo apt-get install -y ros-$ROS_DISTRO-{pkg}", False),
    (r"No executable found|ExecutableNotFound",
     "missing_dep", "ROS2 executable not found",
     "install package and verify source setup.bash", False),
    (r"QoS mismatch|incompatible QoS",
     "ros2_error", "ROS2 QoS mismatch",
     "Match publisher/subscriber QoS or use --qos-reliability best_effort", False),
    (r"no new message|no data",
     "ros2_error", "ROS2 topic has no data — no publisher or QoS mismatch",
     "Check publisher count with ros2 topic info, verify QoS", False),
    (r"Failed to import|ModuleNotFoundError|ImportError",
     "missing_dep", "Python package not installed",
     "pip3 install {module} --break-system-packages", False),
    (r"serial\.SerialException|could not open port|No such file or directory.*tty",
     "serial_error", "Serial port missing or permission denied",
     "ls /dev/tty* to check port, sudo chmod a+rw /dev/ttyACM0", False),
    (r"Permission denied.*tty|permission denied.*serial",
     "permission", "Serial port permission denied",
     "sudo chmod a+rw /dev/ttyACM0 or usermod -a -G dialout $USER", False),
    (r"Permission denied|Operation not permitted",
     "permission", "File/device permission denied",
     "Run with sudo or fix permissions via chmod/chown", False),
    (r"fault_code|FAULT|overcurrent|overvoltage|overtemp",
     "hardware_fault", "Hardware fault detected",
     "Check power/wiring, read fault code, then reset", False),
    (r"timeout after|timed out",
     "ssh_error", "SSH or command timeout",
     "Increase timeout or check board connection", True),
    (r"Connection reset|Broken pipe|ssh.*closed",
     "ssh_error", "SSH connection lost",
     "Reconnect via ssh_connect() then retry", True),
    (r"command not found|No such file.*bin|not installed",
     "missing_dep", "Command or package not installed",
     "apt-get install or install via pip3", False),
]


def parse_error_feedback(result_text: str) -> "dict | None":
    """
    Extract structured error feedback from tool_result text.

    EmbedAgent paper's compiler feedback loop:
    Instead of passing raw errors to the LLM, structure them as type/cause/fix hints
    so the LLM can take corrective action without unnecessary exploration.
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

    return {"error_type": "generic", "root_cause": "Unknown error",
            "suggested_fix": "Check the error message and analyze the root cause", "retry_safe": False}


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
            "root_cause":    "Motion not confirmed — motor deadband or command not delivered",
            "suggested_fix": "Check telemetry, ERPM/speed thresholds, constraints memory",
            "retry_safe":    False,
        },
    }
