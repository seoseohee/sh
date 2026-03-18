"""
ecc_core/consolidation.py — Episodic → Semantic 자동 통합

세션 종료(done()) 또는 주기적으로 호출.
에피소드 실패 패턴을 LLM이 분석해서 failed/constraints namespace에 자동 저장.

참고:
  - Voyager / MetaGPT: 에피소드 트레이스 → 재사용 가능한 스킬/규칙 추상화
  - AgeMem (Yu et al., 2026): 메모리 summarize를 에이전트 도구로
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
    실패 에피소드를 분석해 Semantic Memory에 자동 통합.

    Args:
      memory:       ECCMemory 인스턴스
      goal:         이번 세션의 goal (컨텍스트용)
      client:       Anthropic 클라이언트
      min_failures: 이 수 미만이면 패턴 추출 생략 (노이즈 방지)

    Returns:
      {"failed": N, "constraints": M}  — 저장된 항목 수
    """
    failed_eps = [e for e in memory.episodic if not e.ok]
    if len(failed_eps) < min_failures:
        return {"failed": 0, "constraints": 0}

    # 인과 체인 재구성 — 연결된 실패 묶음
    ep_text = "\n".join(
        f"turn={e.turn} tool={e.tool} caused_by={e.caused_by!r}: {e.summary[:100]}"
        for e in failed_eps[-25:]
    )

    prompt = f"""임베디드 보드 자동화 세션에서 발생한 실패 에피소드를 분석해라.

Goal: {goal[:200]}

실패 에피소드 (최근 순):
{ep_text}

아래 JSON만 출력하라. 다른 텍스트 없이.
{{
  "failed_patterns": [
    {{"key": "짧은_식별자", "value": "실패 이유와 피해야 할 접근법 (한국어, 80자 이내)"}}
  ],
  "discovered_constraints": [
    {{"key": "제약_이름", "value": "구체적 수치 또는 조건"}}
  ]
}}

규칙:
- failed_patterns: 반복된 실패 또는 중요한 단일 실패만 포함. 최대 5개.
- discovered_constraints: 물리적 한계(min_erpm, max_speed 등)가 에피소드에서 명확히 드러난 경우만.
- 에피소드가 불충분하면 빈 리스트 반환.
- 이미 알려진 정보 중복 저장 금지."""

    try:
        resp = client.messages.create(
            model=_consolidation_model(),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip() if resp.content else ""
        # JSON 파싱
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
            # 이미 있는 키는 덮어쓰지 않음
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
            f"\n  🧠 에피소드→시맨틱 통합: "
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
    검증된 스크립트를 Procedural Memory(skill namespace)에 저장.
    executor.py의 _done() 또는 성공적인 script 실행 후 호출 가능.
    """
    if not key or not script_code.strip():
        return
    existing = memory.semantic.get("skill", key)
    if existing:
        return  # 이미 있으면 덮어쓰지 않음
    memory.remember("skill", key, {
        "code":        script_code[:2000],
        "description": description[:200],
        "created":     __import__("time").time(),
    })
