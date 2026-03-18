"""ecc_core/escalation.py — EscalationTracker.

반복 실패 감지 → opus+thinking 자동 전환.
트리거 조건 (OR):
  1. verify FAIL 2회 연속  (ECC_ESCALATE_VERIFY_STREAK)
  2. 직전 2개 tool_result에 동일한 하드웨어 실패 패턴  (ECC_ESCALATE_PATTERN_COUNT)
  3. 같은 bash 명령 3회 이상 반복 (polling 제외)  (ECC_ESCALATE_BASH_REPEAT)
"""
import os


def _esc_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


class EscalationTracker:
    POLLING_KEYWORDS = ("hz", "echo --once", "topic echo", "ps aux", "is-active", "ping")
    FAIL_KEYWORDS    = (
        "exit code 1", "exit code 255", "no data",
        "speed: 0.0", "speed=0.0", "0 publishers",
        "rc=-1", "timed out", "no response",
    )

    def __init__(self):
        self._verify_fail_streak: int           = 0
        self._recent_results:     list[str]     = []
        self._bash_counter:       dict[str, int] = {}

    def record_tool_results(self, tool_blocks: list, results: dict[str, str]) -> None:
        for block in tool_blocks:
            out = results.get(block.id, "")
            if block.name == "ssh_connect":
                continue
            if block.name == "verify":
                self._verify_fail_streak = (
                    self._verify_fail_streak + 1
                    if ("FAIL" in out or "fail" in out.lower())
                    else 0
                )
            if block.name == "bash":
                cmd = block.input.get("command", "")
                if not any(kw in cmd for kw in self.POLLING_KEYWORDS):
                    self._bash_counter[cmd] = self._bash_counter.get(cmd, 0) + 1
            if out:
                self._recent_results.append(out.lower()[:300])
                if len(self._recent_results) > 4:
                    self._recent_results.pop(0)

    def should_escalate(self) -> tuple[bool, str]:
        verify_streak   = _esc_int("ECC_ESCALATE_VERIFY_STREAK", 2)
        pattern_count   = _esc_int("ECC_ESCALATE_PATTERN_COUNT", 2)
        bash_repeat     = _esc_int("ECC_ESCALATE_BASH_REPEAT",   3)

        if self._verify_fail_streak >= verify_streak:
            return True, f"verify FAIL {self._verify_fail_streak}회 연속"
        if len(self._recent_results) >= pattern_count:
            last_n = self._recent_results[-pattern_count:]
            for kw in self.FAIL_KEYWORDS:
                if all(kw in r for r in last_n):
                    return True, f"동일 실패 패턴: '{kw}'"
        for cmd, count in self._bash_counter.items():
            if count >= bash_repeat:
                return True, f"bash {count}회 반복: '{cmd[:60]}'"
        return False, ""

    def get_recent_results(self) -> list[str]:
        return list(self._recent_results)

    def reset_escalation(self) -> None:
        self._verify_fail_streak = 0
        self._bash_counter.clear()
        self._recent_results.clear()
