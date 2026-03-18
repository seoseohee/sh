"""
ecc_core/compactor.py

환경변수:
  ECC_COMPACT_MODEL   압축용 모델 (기본: ECC_MODEL, 없으면 claude-sonnet-4-6)
  ECC_CONTEXT_LIMIT   컨텍스트 토큰 한계 (기본: 모델별 자동 설정)

수정 이력:
  v2 — 한국어 토큰 추정 오차 수정.
       기존 chars // 4 는 영어 기준 (1 token ≈ 4 chars).
       한국어는 1 token ≈ 1~2 chars 이므로 추정치가 실제의 절반 이하.
       결과: 압축 트리거가 너무 늦게 발동 → BadRequestError.
       수정: 문자 유형별 가중치를 적용하는 estimate_tokens() 함수.
"""

import os
import re
import anthropic

COMPACT_TRIGGER_RATIO = 0.85

_DEFAULT_LIMIT = 180_000


def _compact_model() -> str:
    main = os.environ.get("ECC_MODEL", "claude-sonnet-4-6")
    return os.environ.get("ECC_COMPACT_MODEL", main)

def _context_limit() -> int:
    env = os.environ.get("ECC_CONTEXT_LIMIT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return _DEFAULT_LIMIT


def estimate_tokens(messages: list[dict]) -> int:
    """
    메시지 목록의 토큰 수를 추정.

    FIX v2: 언어별 문자-토큰 비율 적용.
      - ASCII (영어/코드): 4 chars ≈ 1 token
      - 한국어/일본어/중국어: 1~2 chars ≈ 1 token → 2 chars per token 사용
      - 그 외 유니코드: 3 chars ≈ 1 token (보수적 추정)

    실제 API 토크나이저와 완전히 일치하지는 않지만
    압축 트리거 판단에 충분한 근사치를 제공.
    """
    total = 0
    for m in messages:
        c = str(m.get("content", ""))
        total += _count_tokens(c)
    return total


def _count_tokens(text: str) -> int:
    """문자열의 토큰 수 추정 (언어 혼용 대응)."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    # CJK Unified Ideographs + Hangul Syllables + Katakana + Hiragana
    cjk_chars = sum(
        1 for ch in text
        if (0xAC00 <= ord(ch) <= 0xD7A3)   # 한글 완성형
        or (0x1100 <= ord(ch) <= 0x11FF)   # 한글 자모
        or (0x4E00 <= ord(ch) <= 0x9FFF)   # CJK 통합 한자
        or (0x3040 <= ord(ch) <= 0x30FF)   # 히라가나/가타카나
    )
    other_chars = len(text) - ascii_chars - cjk_chars

    tokens = (
        ascii_chars // 4       # 영어/코드
        + cjk_chars // 2       # 한/중/일
        + other_chars // 3     # 기타 유니코드
    )
    return max(tokens, len(text) // 4)  # 하한: 기존 방식


def should_compact(messages: list[dict]) -> bool:
    return estimate_tokens(messages) > _context_limit() * COMPACT_TRIGGER_RATIO


def compact(
    messages: list[dict],
    goal: str,
    todo_summary: str,
    client: anthropic.Anthropic,
    persistent_facts: str = "",
) -> list[dict]:
    """
    메시지 히스토리를 압축한다.

    persistent_facts: ECCMemory.get_persistent_facts() 결과.
    물리 제약, 하드웨어 사실 등 압축 후에도 반드시 보존해야 할 정보.
    """
    print("\n  📦 컨텍스트 압축 중...", flush=True)

    history_lines: list[str] = []
    for m in messages[1:]:
        role = m.get("role", "")
        content = m.get("content", "")

        if isinstance(content, str):
            history_lines.append(f"[{role}] {content[:400]}")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    history_lines.append(f"[{role}/text] {block.get('text', '')[:300]}")
                elif btype == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    detail = inp.get("command", inp.get("code", inp.get("path", str(inp))))[:120]
                    history_lines.append(f"[tool:{name}] {detail}")
                elif btype == "tool_result":
                    out = str(block.get("content", ""))[:200]
                    history_lines.append(f"[result] {out}")

    history_text = "\n".join(history_lines[-120:])

    prompt = f"""다음은 임베디드 보드 자동화 작업의 대화 기록이다.

목표(goal): {goal}

대화 기록:
{history_text}

위 기록에서 다음을 추출해서 간결하게 요약하라:
1. 완료된 작업 목록
2. 발견한 하드웨어 정보 (디바이스 경로, IP, 파라미터 등 — 구체적인 값 유지)
3. 실패한 접근법과 실패 이유 (재시도 방지를 위해 중요)
4. 현재 상태 (어디까지 진행됐는가)
5. 남은 작업

600자 이내로, 사실 위주로."""

    try:
        resp = client.messages.create(
            model=_compact_model(),
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = resp.content[0].text if resp.content else "(요약 실패)"
    except Exception as e:
        summary = f"(컨텍스트 압축 중 오류: {e})"

    facts_section = (
        f"[Persistent facts — never rediscover these]\n{persistent_facts}\n\n"
        if persistent_facts.strip() else ""
    )

    compacted: list[dict] = [
        {
            "role": "user",
            "content": (
                f"Goal: {goal}\n\n"
                f"{facts_section}"
                f"[Context summary from previous turns]\n\n"
                f"{summary}\n\n"
                f"[Todo status]\n{todo_summary}\n\n"
                "Continue working toward the goal."
            )
        }
    ]

    print(f"  ✅ {len(messages)} → {len(compacted)} 메시지로 압축", flush=True)
    return compacted
