"""
ecc_core/consolidation.py — Episodic → Semantic auto-consolidation

Called on session end (done()) or periodically.
LLM analyzes failed episode patterns and auto-saves to failed/constraints namespace.

References:
  - Voyager / MetaGPT: episode traces → reusable skills/rules abstraction
  - AgeMem (Yu et al., 2026): memory summarize as agent tool
"""

import json
import os
import re
import anthropic

from .memory import ECCMemory


def _consolidation_model() -> str:
    return os.environ.get("ECC_COMPACT_MODEL",
           os.environ.get("ECC_MODEL", "claude-sonnet-4-6"))


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
      min_failures: skip pattern extraction below this count (noise prevention)

    Returns:
      {"failed": N, "constraints": M}  — number of items saved
    """
    failed_eps = [e for e in memory.episodic if not e.ok]
    if len(failed_eps) < min_failures:
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
        # JSON parse
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
            # Don't overwrite existing keys
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
        return  # Don't overwrite if already exists
    memory.remember("skill", key, {
        "code":        script_code[:2000],
        "description": description[:200],
        "created":     __import__("time").time(),
    })
