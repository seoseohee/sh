"""
ecc_core/escalation.py — EscalationTracker

Changelog:
  v2 — [Improve G] should_ask_user() 메타인지 판단 추가.
         기존: 에스컬레이션(Opus 전환)만 있고, "이 목표 자체가 현재 상태로 불가능하다"는
               판단이 없어 max_turns 상한까지 무한 반복.
         수정:
           - should_ask_user(): 에스컬레이션해도 해결 불가 상황 감지 → ask_user 라우팅 신호
           - 동일 툴+동일 출력 N회 반복 감지 (_same_signature_count)
           - verify FAIL 연속 임계치를 에스컬레이션(2)과 ask_user(5)로 분리
           - ECC_ASK_USER_VERIFY_STREAK / ECC_ASK_USER_SAME_SIG 환경변수로 조정 가능
"""
import os
import hashlib


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
        self._verify_fail_streak: int            = 0
        self._recent_results:     list[str]      = []
        self._bash_counter:       dict[str, int] = {}
        # [Improve G] 동일 툴+출력 서명 카운터
        self._sig_counter:        dict[str, int] = {}

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

            # [Improve G] 동일 툴+출력 서명 추적 (폴링 제외)
            if block.name not in ("bash_wait",):
                sig = _tool_output_signature(block.name, out)
                self._sig_counter[sig] = self._sig_counter.get(sig, 0) + 1

            if out:
                self._recent_results.append(out.lower()[:300])
                if len(self._recent_results) > 4:
                    self._recent_results.pop(0)

    def should_escalate(self) -> tuple[bool, str]:
        verify_streak = _esc_int("ECC_ESCALATE_VERIFY_STREAK", 2)
        pattern_count = _esc_int("ECC_ESCALATE_PATTERN_COUNT", 2)
        bash_repeat   = _esc_int("ECC_ESCALATE_BASH_REPEAT",   3)

        if self._verify_fail_streak >= verify_streak:
            return True, f"verify FAIL streak: {self._verify_fail_streak}"
        if len(self._recent_results) >= pattern_count:
            last_n = self._recent_results[-pattern_count:]
            for kw in self.FAIL_KEYWORDS:
                if all(kw in r for r in last_n):
                    return True, f"Same failure pattern: '{kw}'"
        for cmd, count in self._bash_counter.items():
            if count >= bash_repeat:
                return True, f"bash repeated {count}x: '{cmd[:60]}'"
        return False, ""

    def should_ask_user(self) -> tuple[bool, str]:
        """[Improve G] 에스컬레이션해도 해결 불가 상황 감지.

        should_escalate()와 별개로 호출. True 반환 시 loop.py는
        ask_user()를 통해 사용자에게 상황을 보고하고 방향을 물어야 함.

        조건:
          1. verify FAIL이 ask_user 임계치 이상 (기본 5회, 에스컬레이션 임계 2회보다 높음)
          2. 동일 툴+출력 서명이 N회 이상 반복 (기본 4회)
             → Opus로 에스컬레이션해도 동일한 결과가 나오는 상황

        환경변수:
          ECC_ASK_USER_VERIFY_STREAK : verify FAIL 임계치 (기본 5)
          ECC_ASK_USER_SAME_SIG      : 동일 서명 반복 임계치 (기본 4)
        """
        ask_verify  = _esc_int("ECC_ASK_USER_VERIFY_STREAK", 5)
        ask_same_sig = _esc_int("ECC_ASK_USER_SAME_SIG", 4)

        if self._verify_fail_streak >= ask_verify:
            return True, (
                f"verify FAIL {self._verify_fail_streak}회 연속 — "
                "하드웨어 물리 상태 직접 확인이 필요합니다."
            )

        for sig, count in self._sig_counter.items():
            if count >= ask_same_sig:
                tool = sig.split(":")[0]
                return True, (
                    f"{tool} 툴이 동일한 결과를 {count}회 반복 — "
                    "목표 또는 접근 방법을 재정의해야 합니다."
                )

        return False, ""

    def get_recent_results(self) -> list[str]:
        return list(self._recent_results)

    def reset_escalation(self) -> None:
        self._verify_fail_streak = 0
        self._bash_counter.clear()
        self._recent_results.clear()
        # [Improve G] sig_counter는 세션 전반 축적 정보이므로 에스컬레이션 리셋 시 초기화 안 함
        # ask_user 발생 후 사용자가 새 목표를 주면 루프에서 수동으로 reset_all() 호출


    def reset_all(self) -> None:
        """새 목표 시작 시 완전 초기화."""
        self.reset_escalation()
        self._sig_counter.clear()


def _tool_output_signature(tool_name: str, output: str) -> str:
    """툴 이름 + 출력 앞 200자의 MD5. 동일 반복 감지용."""
    key = f"{tool_name}:{output[:200]}"
    return tool_name + ":" + hashlib.md5(key.encode()).hexdigest()[:8]