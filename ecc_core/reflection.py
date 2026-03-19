"""
ecc_core/reflection.py

Changelog:
  v2 — [Fix 2] generate_reflection() docstring 위치 버그 수정.
  v3 — [Improve C] FAILURE_ROUTING 정규식화 + LLM fallback 분류.
         기존: 고정 문자열 부분 일치 (e.g. "speed: 0.0" 은 감지, "speed=0.00" 미감지).
         수정:
           1. 각 패턴을 re.compile()로 컴파일 → 변형 패턴 포괄.
           2. 모든 키워드 매칭 실패 시 LLM이 실패 유형을 직접 분류.
           3. LLM fallback은 client가 주어진 경우만 실행 (subagent 등 client 없는 호출 안전).
           4. _classify_via_llm() 결과를 캐시 (동일 내용 반복 호출 방지).
"""

import os
import re
import hashlib
import anthropic
from dataclasses import dataclass


class ReplanDecision:
    RETRY_SAME_TASK   = "retry"
    REVISE_TASK_GRAPH = "revise"
    REPLAN_FROM_ROOT  = "replan"


@dataclass
class RecoveryDecision:
    route: str
    note: str
    reason: str = ""


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
    return _VERIFIER_ROUTING.get(
        verifier_reason,
        RecoveryDecision(
            ReplanDecision.REPLAN_FROM_ROOT,
            "Unknown failure; reflect and replan from root.",
            verifier_reason,
        ),
    )


# ─────────────────────────────────────────────────────────────
# [Improve C] 정규식 기반 FAILURE_ROUTING
# ─────────────────────────────────────────────────────────────
#
# 기존 문제:
#   ("speed: 0.0", ...) — "speed=0.0", "Speed: 0.00" 미감지
#   ("command not found", ...) — "bash: XXX: command not found" 는 감지하지만
#                                "not found" 단독은 미감지 → 이제 포괄
#
# 각 항목: (compiled_regex, ReplanDecision)
# 순서 중요: 더 구체적인 패턴이 앞에 위치해야 함.

_FAILURE_ROUTING_RE: list[tuple[re.Pattern, str]] = [
    # ── Retry (일시적 오류) ────────────────────────────────────
    (re.compile(r"timeout\s+after|timed\s+out", re.I),           ReplanDecision.RETRY_SAME_TASK),
    (re.compile(r"connection\s+refused",        re.I),           ReplanDecision.RETRY_SAME_TASK),
    (re.compile(r"no\s+route\s+to\s+host",      re.I),           ReplanDecision.RETRY_SAME_TASK),
    (re.compile(r"ssh_connect\s+failed",        re.I),           ReplanDecision.RETRY_SAME_TASK),
    (re.compile(r"\brc=-1\b",                   re.I),           ReplanDecision.RETRY_SAME_TASK),
    (re.compile(r"broken\s+pipe|connection\s+reset", re.I),      ReplanDecision.RETRY_SAME_TASK),

    # ── Revise (물리적 제약 / QoS / 데이터 없음) ─────────────
    (re.compile(r"speed\s*[=:]\s*0+\.?0*\b",   re.I),           ReplanDecision.REVISE_TASK_GRAPH),
    (re.compile(r"\bno\s+data\b",               re.I),           ReplanDecision.REVISE_TASK_GRAPH),
    (re.compile(r"\b0\s+publishers?\b",         re.I),           ReplanDecision.REVISE_TASK_GRAPH),
    (re.compile(r"\bdeadband\b",                re.I),           ReplanDecision.REVISE_TASK_GRAPH),
    (re.compile(r"below\s+minimum|min_erpm",    re.I),           ReplanDecision.REVISE_TASK_GRAPH),
    (re.compile(r"qos\s+mismatch|incompatible\s+qos", re.I),     ReplanDecision.REVISE_TASK_GRAPH),
    (re.compile(r"no\s+new\s+message",          re.I),           ReplanDecision.REVISE_TASK_GRAPH),
    (re.compile(r"erpm.*0\b|0.*erpm",           re.I),           ReplanDecision.REVISE_TASK_GRAPH),

    # ── Replan (구조적 불가능) ────────────────────────────────
    (re.compile(r"command\s+not\s+found|not\s+installed", re.I), ReplanDecision.REPLAN_FROM_ROOT),
    (re.compile(r"no\s+such\s+file|file\s+not\s+found",   re.I), ReplanDecision.REPLAN_FROM_ROOT),
    (re.compile(r"permission\s+denied|operation\s+not\s+permitted", re.I), ReplanDecision.REPLAN_FROM_ROOT),
    (re.compile(r"exit\s+code\s+127",           re.I),           ReplanDecision.REPLAN_FROM_ROOT),
    (re.compile(r"import\s+error|module\s+not\s+found",    re.I), ReplanDecision.REPLAN_FROM_ROOT),
]

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

# LLM fallback 결과 캐시 (동일 내용 반복 호출 방지)
_llm_classify_cache: dict[str, str] = {}


def classify_failure(
    recent_results: list[str],
    client: "anthropic.Anthropic | None" = None,
) -> str:
    """실패 유형을 정규식으로 분류. 미감지 시 LLM fallback.

    [Improve C]
      - 정규식으로 변형 패턴 포괄 (예: speed=0.0, Speed: 0.00 모두 감지)
      - 모든 패턴 미감지 시 client가 있으면 LLM으로 의미 기반 분류
      - LLM 호출 결과는 캐시에 저장해 반복 호출 방지
    """
    combined = " ".join(r.lower() for r in recent_results[-4:])

    for pattern, decision in _FAILURE_ROUTING_RE:
        if pattern.search(combined):
            return decision

    # LLM fallback
    if client is not None:
        return _classify_via_llm(combined, client)

    return ReplanDecision.REPLAN_FROM_ROOT


def _classify_via_llm(text: str, client: anthropic.Anthropic) -> str:
    """키워드 매칭 실패 시 LLM이 실패 유형 직접 분류.

    캐시 키: 텍스트의 MD5 (내용 동일 시 재호출 방지).
    """
    cache_key = hashlib.md5(text[:500].encode()).hexdigest()
    if cache_key in _llm_classify_cache:
        return _llm_classify_cache[cache_key]

    model = os.environ.get("ECC_COMPACT_MODEL",
            os.environ.get("ECC_MODEL", "claude-sonnet-4-6"))
    prompt = (
        "Classify the following embedded board agent failure into exactly one category.\n\n"
        f"Failure text:\n{text[:400]}\n\n"
        "Categories (output ONLY the category name, nothing else):\n"
        f"  {ReplanDecision.RETRY_SAME_TASK}  — transient: timeout, SSH drop, rate limit\n"
        f"  {ReplanDecision.REVISE_TASK_GRAPH} — physical constraint: speed=0, deadband, no data\n"
        f"  {ReplanDecision.REPLAN_FROM_ROOT}  — structural: missing package, permission denied\n"
    )
    try:
        resp = client.messages.create(
            model=model, max_tokens=20,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = (resp.content[0].text if resp.content else "").strip().lower()
        if ReplanDecision.RETRY_SAME_TASK in raw:
            result = ReplanDecision.RETRY_SAME_TASK
        elif ReplanDecision.REVISE_TASK_GRAPH in raw:
            result = ReplanDecision.REVISE_TASK_GRAPH
        else:
            result = ReplanDecision.REPLAN_FROM_ROOT
    except Exception:
        result = ReplanDecision.REPLAN_FROM_ROOT

    _llm_classify_cache[cache_key] = result
    return result


# ─────────────────────────────────────────────────────────────
# Reflection generation
# ─────────────────────────────────────────────────────────────

def generate_reflection(
    messages: list[dict],
    goal: str,
    failure_type: str,
    client: anthropic.Anthropic,
    model: str = "",
) -> str:
    """Reflexion pattern — verbal failure reason 생성."""
    if not model:
        model = os.environ.get("ECC_MODEL", "claude-sonnet-4-6")

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
            model=model, max_tokens=250, system=system,
            messages=recent + [{"role": "user", "content": prompt}],
        )
        return resp.content[0].text if resp.content else "(reflection unavailable)"
    except Exception as e:
        return f"(reflection error: {e})"


def make_reflection_message(reflection_text: str, decision: str) -> dict:
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