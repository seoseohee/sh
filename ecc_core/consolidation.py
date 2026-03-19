"""
ecc_core/consolidation.py — Episodic → Semantic auto-consolidation

Called on session end (done()) or periodically.
LLM analyzes failed episode patterns and auto-saves to failed/constraints namespace.

References:
  - Voyager / MetaGPT: episode traces → reusable skills/rules abstraction
  - AgeMem (Yu et al., 2026): memory summarize as agent tool

Changelog:
  v2 — [Fix] 임계값 하드코딩 제거.
         기존: `importance >= 0.9` 리터럴이 _TOOL_IMPORTANCE 상수와 암묵적 결합.
               _TOOL_IMPORTANCE 값 변경 시 의도치 않은 동작 변경 가능.
         수정: CRITICAL_IMPORTANCE_THRESHOLD 명시적 상수로 분리.
               ECC_CRITICAL_IMPORTANCE_THRESHOLD 환경변수로 오버라이드 가능.
               docstring으로 어떤 tool이 해당 임계값에 걸리는지 명시.
"""

import json
import os
import re
import anthropic

from .memory import ECCMemory


def _consolidation_model() -> str:
    return os.environ.get("ECC_COMPACT_MODEL",
           os.environ.get("ECC_MODEL", "claude-sonnet-4-6"))


# ─────────────────────────────────────────────────────────────
# [Fix] 임계값 상수 — _TOOL_IMPORTANCE와의 결합을 명시적으로 관리
# ─────────────────────────────────────────────────────────────

def _critical_importance_threshold() -> float:
    """단발성 치명 실패의 min_failures 우회 임계값.

    기본값 0.9의 근거 (_TOOL_IMPORTANCE 기준):
      임계값 이상 (≥ 0.9):
        remember     = 0.9   → 실패 시 0.9  (우회 대상 ✓)
        ssh_connect  = 0.9   → 실패 시 1.0  (우회 대상 ✓)
        done         = 1.0   → 실패 시 1.0  (우회 대상 ✓)
        verify(실패) = 0.8 + 0.2 보정 = 1.0 (우회 대상 ✓)
        probe(실패)  = 0.8 + 0.2 보정 = 1.0 (우회 대상 ✓)

      임계값 미만 (< 0.9):
        script(실패) = 0.5 + 0.2 = 0.7  (우회 안 됨 ✗ — 오탐 방지)
        bash(실패)   = 0.3 + 0.2 = 0.5  (우회 안 됨 ✗ — 오탐 방지)
        todo(실패)   = 0.2 + 0.2 = 0.4  (우회 안 됨 ✗ — 오탐 방지)

    _TOOL_IMPORTANCE 기본값을 변경할 경우 이 임계값도 함께 검토 필요.
    ECC_CRITICAL_IMPORTANCE_THRESHOLD 환경변수로 런타임 오버라이드 가능.
    """
    try:
        return float(os.environ.get("ECC_CRITICAL_IMPORTANCE_THRESHOLD", 0.9))
    except (ValueError, TypeError):
        return 0.9


def consolidate_episodic(
    memory: ECCMemory,
    goal:   str,
    client: anthropic.Anthropic,
    min_failures: int = 3,
) -> dict[str, int]:
    """
    Analyze failed episodes and auto-consolidate into Semantic Memory.

    Args:
      memory:       ECCMemory instance
      goal:         goal for this session (for context)
      client:       Anthropic client
      min_failures: skip pattern extraction below this count (noise prevention).
                    단, importance >= CRITICAL_IMPORTANCE_THRESHOLD인 치명 실패가
                    하나라도 있으면 개수 무관하게 통합.

    Returns:
      {"failed": N, "constraints": M}  — number of items saved
    """
    failed_eps = [e for e in memory.episodic if not e.ok]

    # [Fix] 하드코딩 0.9 → 함수 호출로 교체
    # 임계값과 해당하는 tool 목록은 _critical_importance_threshold() docstring 참조
    threshold    = _critical_importance_threshold()
    has_critical = any(e.importance >= threshold for e in failed_eps)

    if len(failed_eps) < min_failures and not has_critical:
        return {"failed": 0, "constraints": 0}

    # Reconstruct causal chain — linked failure groups
    ep_text = "\n".join(
        f"turn={e.turn} tool={e.tool} caused_by={e.caused_by!r}: {e.summary[:100]}"
        for e in failed_eps[-25:]
    )

    prompt = f"""Analyze failed episodes from an embedded board automation session.

Goal: {goal[:200]}

Failed episodes (most recent first):
{ep_text}

Output only the JSON below. No other text.
{{
  "failed_patterns": [
    {{"key": "short_key", "value": "reason for failure and what to avoid (80 chars max)"}}
  ],
  "discovered_constraints": [
    {{"key": "constraint_name", "value": "specific value or condition"}}
  ]
}}

Rules:
- failed_patterns: include only repeated failures or significant single failures. Max 5.
- discovered_constraints: only when physical limits (min_erpm, max_speed etc.) are clearly evidenced.
- Return empty lists if episodes are insufficient.
- Do not store already-known information."""

    try:
        resp = client.messages.create(
            model=_consolidation_model(),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip() if resp.content else ""
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return {"failed": 0, "constraints": 0}
        data = json.loads(m.group())
    except Exception:
        return {"failed": 0, "constraints": 0}

    saved_failed      = 0
    saved_constraints = 0

    for item in data.get("failed_patterns", []):
        key = str(item.get("key", "")).strip()
        val = str(item.get("value", "")).strip()
        if key and val:
            if not memory.semantic.get("failed", key):
                memory.remember("failed", key, val)
                saved_failed += 1

    for item in data.get("discovered_constraints", []):
        key = str(item.get("key", "")).strip()
        val = item.get("value")
        if key and val is not None:
            if not memory.semantic.get("constraints", key):
                memory.remember("constraints", key, val)
                saved_constraints += 1

    if saved_failed + saved_constraints > 0:
        print(
            f"\n  🧠 episodic→semantic consolidation: "
            f"failed={saved_failed}, constraints={saved_constraints}",
            flush=True,
        )

    return {"failed": saved_failed, "constraints": saved_constraints}


def consolidate_skill(
    memory:      ECCMemory,
    script_code: str,
    description: str,
    key:         str,
) -> None:
    """
    Save a validated script to Procedural Memory (skill namespace).
    Can be called from executor.py _done() or after successful script execution.
    """
    if not key or not script_code.strip():
        return
    existing = memory.semantic.get("skill", key)
    if existing:
        return
    memory.remember("skill", key, {
        "code":        script_code[:2000],
        "description": description[:200],
        "created":     __import__("time").time(),
    })