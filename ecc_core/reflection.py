"""
ecc_core/reflection.py

Reflexion pattern implementation (Shinn et al., NeurIPS 2023)

Classify failure type and decide 3-way replanning destination:
  RETRY_SAME_TASK   — retry with adjusted params (transient errors: connection/timeout)
  REVISE_TASK_GRAPH — keep goal, revise sub-tasks (physical constraint discovered)
  REPLAN_FROM_ROOT  — reinterpret goal, return to LLM Planner (structurally impossible)

classify_failure()  : classify failure type from recent tool_result text
generate_reflection(): call LLM to generate verbal failure analysis
make_reflection_message(): create user message to inject into loop.py messages

Changelog:
  v2 — [Fix 2] generate_reflection() docstring 위치 버그 수정
         기존: if not model 블록 다음에 docstring이 위치
               → 문자열 리터럴로만 처리되어 help()/IDE 문서화가 깨짐
         수정: docstring을 함수 첫 줄로 이동, model 기본값 처리는 그 아래로
"""

import os
import anthropic
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────
# Replan Decision constants + typed dataclass
# ─────────────────────────────────────────────────────────────

class ReplanDecision:
    RETRY_SAME_TASK   = "retry"   # same tool, change parameters only
    REVISE_TASK_GRAPH = "revise"  # keep goal, switch method
    REPLAN_FROM_ROOT  = "replan"  # full strategy reset


@dataclass
class RecoveryDecision:
    """Carry classified failure type + action hint together.

    route: ReplanDecision constant (retry / revise / replan)
    note:  concrete action guidance for the LLM
    reason: verifier.py reason string (optional)
    """
    route: str
    note: str
    reason: str = ""


# ─────────────────────────────────────────────────────────────
# verifier reason → RecoveryDecision mapping
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
    """verifier.py reason → RecoveryDecision."""
    return _VERIFIER_ROUTING.get(
        verifier_reason,
        RecoveryDecision(
            ReplanDecision.REPLAN_FROM_ROOT,
            "Unknown failure; reflect and replan from root.",
            verifier_reason,
        ),
    )


# failure keyword → replan destination
FAILURE_ROUTING: list[tuple[str, str]] = [
    # ── transient connection/execution errors → retry
    ("timeout after",        ReplanDecision.RETRY_SAME_TASK),
    ("connection refused",   ReplanDecision.RETRY_SAME_TASK),
    ("no route to host",     ReplanDecision.RETRY_SAME_TASK),
    ("ssh_connect failed",   ReplanDecision.RETRY_SAME_TASK),
    ("rc=-1",                ReplanDecision.RETRY_SAME_TASK),

    # ── physical constraint found → revise method
    ("speed: 0.0",           ReplanDecision.REVISE_TASK_GRAPH),
    ("speed=0.0",            ReplanDecision.REVISE_TASK_GRAPH),
    ("no data",              ReplanDecision.REVISE_TASK_GRAPH),
    ("0 publishers",         ReplanDecision.REVISE_TASK_GRAPH),
    ("deadband",             ReplanDecision.REVISE_TASK_GRAPH),
    ("below minimum",        ReplanDecision.REVISE_TASK_GRAPH),
    ("min_erpm",             ReplanDecision.REVISE_TASK_GRAPH),
    ("qos mismatch",         ReplanDecision.REVISE_TASK_GRAPH),
    ("no new message",       ReplanDecision.REVISE_TASK_GRAPH),

    # ── structurally impossible → full strategy reset
    ("command not found",    ReplanDecision.REPLAN_FROM_ROOT),
    ("not installed",        ReplanDecision.REPLAN_FROM_ROOT),
    ("no such file",         ReplanDecision.REPLAN_FROM_ROOT),
    ("permission denied",    ReplanDecision.REPLAN_FROM_ROOT),
    ("exit code 127",        ReplanDecision.REPLAN_FROM_ROOT),
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


# ─────────────────────────────────────────────────────────────
# Failure classification
# ─────────────────────────────────────────────────────────────

def classify_failure(recent_results: list[str]) -> str:
    """Classify failure type from recent tool_result text.

    No match → REPLAN_FROM_ROOT (unknown, LLM decides).
    """
    combined = " ".join(r.lower() for r in recent_results[-4:])
    for keyword, decision in FAILURE_ROUTING:
        if keyword in combined:
            return decision
    return ReplanDecision.REPLAN_FROM_ROOT


# ─────────────────────────────────────────────────────────────
# Reflection generation (LLM call)
# ─────────────────────────────────────────────────────────────

def generate_reflection(
    messages: list[dict],
    goal: str,
    failure_type: str,
    client: anthropic.Anthropic,
    model: str = "",
) -> str:
    """Reflexion pattern — generate verbal failure reason.

    Called when EscalationTracker fires.
    Result injected into messages is seen by LLM next turn.

    [Fix 2] docstring을 함수 첫 줄로 이동.
    기존 코드에서는 `if not model:` 블록 다음에 docstring이 위치해
    Python이 이를 docstring이 아닌 문자열 리터럴로 처리했음.
    """
    # [Fix 2] model 기본값 처리 — docstring 이후로 이동
    if not model:
        model = os.environ.get("ECC_MODEL", "claude-sonnet-4-6")

    # Use only last 6 messages (cost saving)
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
# Create message for injection into messages
# ─────────────────────────────────────────────────────────────

def make_reflection_message(reflection_text: str, decision: str) -> dict:
    """Inject reflection result as user message into loop.py messages.

    Agent sees this next turn and takes a different approach.
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