"""
ecc_core/reflection.py

Reflexion 패턴 구현 (Shinn et al., NeurIPS 2023)

실패 유형을 분류하고 3-way replanning 목적지 결정:
  RETRY_SAME_TASK   — 파라미터만 바꿔서 재시도 (연결/타임아웃 등 일시 오류)
  REVISE_TASK_GRAPH — goal 유지, 하위 task 수정 (물리 제약 발견)
  REPLAN_FROM_ROOT  — goal 재해석, LLM Planner 복귀 (구조적 불가능)

classify_failure()  : 최근 tool_result 텍스트에서 실패 유형 분류
generate_reflection(): LLM 호출로 "왜 실패했는가" 언어 생성
make_reflection_message(): loop.py messages에 주입할 user 메시지 생성
"""

import anthropic


# ─────────────────────────────────────────────────────────────
# Replan Decision 상수 + typed dataclass
# ─────────────────────────────────────────────────────────────

from dataclasses import dataclass

class ReplanDecision:
    RETRY_SAME_TASK   = "retry"   # 같은 도구, 파라미터만 변경
    REVISE_TASK_GRAPH = "revise"  # goal 유지, 방법 전환
    REPLAN_FROM_ROOT  = "replan"  # 전략 재수립


@dataclass
class RecoveryDecision:
    """분류된 실패 유형 + 행동 힌트를 함께 전달.

    route: ReplanDecision 상수 (retry / revise / replan)
    note:  LLM에게 전달할 구체적 행동 지침
    reason: verifier.py의 reason 문자열 (선택)
    """
    route: str
    note: str
    reason: str = ""


# ─────────────────────────────────────────────────────────────
# verifier reason → RecoveryDecision 매핑
# (recovery.py 패턴을 reflection.py에 통합)
# ─────────────────────────────────────────────────────────────

_VERIFIER_ROUTING: dict[str, RecoveryDecision] = {
    "execution_error": RecoveryDecision(
        ReplanDecision.RETRY_SAME_TASK,
        "Retry with adjusted parameters or safer inspection first.",
        "execution_error",
    ),
    "no_observable_output": RecoveryDecision(
        ReplanDecision.REVISE_TASK_GRAPH,
        "Result is ambiguous. Add observation or verification step.",
        "no_observable_output",
    ),
    "verification_weak": RecoveryDecision(
        ReplanDecision.REPLAN_FROM_ROOT,
        "Current plan lacks strong evidence. Replan with explicit verification.",
        "verification_weak",
    ),
    "motion_not_verified": RecoveryDecision(
        ReplanDecision.REPLAN_FROM_ROOT,
        "Motion goal not achieved or not observed. Replan from root.",
        "motion_not_verified",
    ),
}


def route_from_verifier(verifier_reason: str) -> RecoveryDecision:
    """verifier.py reason → RecoveryDecision.

    verify_execution() 결과를 바로 recovery decision으로 변환.
    """
    return _VERIFIER_ROUTING.get(
        verifier_reason,
        RecoveryDecision(
            ReplanDecision.REPLAN_FROM_ROOT,
            "Unknown failure; reflect and replan from root.",
            verifier_reason,
        ),
    )


# 실패 키워드 → replan 목적지
# 매칭 우선순위: 위에서 아래로
FAILURE_ROUTING: list[tuple[str, str]] = [
    # ── 일시적 연결/실행 문제 → 재시도
    ("timeout after",        ReplanDecision.RETRY_SAME_TASK),
    ("connection refused",   ReplanDecision.RETRY_SAME_TASK),
    ("no route to host",     ReplanDecision.RETRY_SAME_TASK),
    ("ssh_connect failed",   ReplanDecision.RETRY_SAME_TASK),
    ("rc=-1",                ReplanDecision.RETRY_SAME_TASK),

    # ── 물리 제약 발견 → 방법 수정
    ("speed: 0.0",           ReplanDecision.REVISE_TASK_GRAPH),
    ("speed=0.0",            ReplanDecision.REVISE_TASK_GRAPH),
    ("no data",              ReplanDecision.REVISE_TASK_GRAPH),
    ("0 publishers",         ReplanDecision.REVISE_TASK_GRAPH),
    ("deadband",             ReplanDecision.REVISE_TASK_GRAPH),
    ("below minimum",        ReplanDecision.REVISE_TASK_GRAPH),
    ("min_erpm",             ReplanDecision.REVISE_TASK_GRAPH),
    ("qos mismatch",         ReplanDecision.REVISE_TASK_GRAPH),
    ("no new message",       ReplanDecision.REVISE_TASK_GRAPH),

    # ── 구조적 불가능 → 전략 재수립
    ("command not found",    ReplanDecision.REPLAN_FROM_ROOT),
    ("not installed",        ReplanDecision.REPLAN_FROM_ROOT),
    ("no such file",         ReplanDecision.REPLAN_FROM_ROOT),
    ("permission denied",    ReplanDecision.REPLAN_FROM_ROOT),
    ("exit code 127",        ReplanDecision.REPLAN_FROM_ROOT),
]

# REPLAN 결정에 따른 액션 힌트
_ACTION_HINT: dict[str, str] = {
    ReplanDecision.RETRY_SAME_TASK: (
        "The failure looks transient (connection/timeout). "
        "Retry with adjusted parameters or increased timeout."
    ),
    ReplanDecision.REVISE_TASK_GRAPH: (
        "A physical constraint was discovered. "
        "Keep the goal but change the approach. "
        "Do NOT send the same command again."
    ),
    ReplanDecision.REPLAN_FROM_ROOT: (
        "The approach may be fundamentally wrong. "
        "Reconsider the strategy from scratch."
    ),
}


# ─────────────────────────────────────────────────────────────
# 실패 분류
# ─────────────────────────────────────────────────────────────

def classify_failure(recent_results: list[str]) -> str:
    """
    최근 tool_result 텍스트에서 실패 유형 분류.
    매칭 없으면 REPLAN_FROM_ROOT (unknown → LLM이 판단).
    """
    combined = " ".join(r.lower() for r in recent_results[-4:])
    for keyword, decision in FAILURE_ROUTING:
        if keyword in combined:
            return decision
    return ReplanDecision.REPLAN_FROM_ROOT


# ─────────────────────────────────────────────────────────────
# Reflection 생성 (LLM 호출)
# ─────────────────────────────────────────────────────────────

def generate_reflection(
    messages: list[dict],
    goal: str,
    failure_type: str,
    client: anthropic.Anthropic,
    model: str = "claude-sonnet-4-6",
) -> str:
    """
    Reflexion 패턴 — 실패 이유를 언어로 생성.
    EscalationTracker 트리거 시 호출.
    결과를 messages에 주입하면 다음 turn LLM에 반영됨.
    """
    # 최근 6개 메시지만 사용 (비용 절약)
    recent = messages[-6:] if len(messages) >= 6 else messages

    system = (
        "You are reviewing your own recent actions on an embedded board.\n"
        "Be concrete: cite exact error messages, device paths, error codes.\n"
        "Max 120 words. No preamble."
    )

    hint = _ACTION_HINT.get(failure_type, "Try a different approach.")

    prompt = (
        f"Goal: {goal[:200]}\n"
        f"Failure type: {failure_type}\n"
        f"Hint: {hint}\n\n"
        "Analyze concisely:\n"
        "1. What failed (exact error)\n"
        "2. Root cause\n"
        "3. What to try differently"
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=250,
            system=system,
            messages=recent + [{"role": "user", "content": prompt}],
        )
        return resp.content[0].text if resp.content else "(reflection unavailable)"
    except Exception as e:
        return f"(reflection error: {e})"


# ─────────────────────────────────────────────────────────────
# messages 주입용 메시지 생성
# ─────────────────────────────────────────────────────────────

def make_reflection_message(reflection_text: str, decision: str) -> dict:
    """
    reflection 결과를 loop.py messages에 user 메시지로 주입.
    에이전트가 다음 turn에 이 내용을 참조해서 다른 접근을 취한다.
    """
    action = {
        ReplanDecision.RETRY_SAME_TASK:   "Retry with adjusted parameters.",
        ReplanDecision.REVISE_TASK_GRAPH: "Revise your approach — keep the goal, change the method.",
        ReplanDecision.REPLAN_FROM_ROOT:  "Reconsider your strategy from scratch.",
    }.get(decision, "Try a different approach.")

    return {
        "role": "user",
        "content": (
            f"[Self-reflection — {decision}]\n"
            f"{reflection_text}\n\n"
            f"→ Next action: {action}"
        ),
    }
