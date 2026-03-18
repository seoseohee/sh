"""
ecc_core/compactor.py

Environment variables:
  ECC_COMPACT_MODEL      압축 모델 (default: ECC_MODEL, fallback: claude-sonnet-4-6)
  ECC_CONTEXT_LIMIT      컨텍스트 토큰 한계 (default: 모델별 자동)
  ECC_COMPACT_TRIGGER    압축 트리거 비율 0~1 (default: 0.85)
  ECC_COMPACT_MAX_TOKENS 압축 LLM 응답 한도 (default: 1500)

Changelog:
  v2 — 한국어 토큰 추정 오류 수정.
       기존 chars // 4는 영어 기준(1 token ≈ 4 chars).
       한국어는 1 token ≈ 1~2 chars라서 실제의 절반 이하로 추정됨.
       결과: 압축 트리거 지연 → BadRequestError.
       수정: estimate_tokens()에 언어별 가중치 적용.
  v3 — COMPACT_TRIGGER_RATIO, max_tokens env 오버라이드 추가.
       max_tokens 800 → 1500으로 상향 (긴 세션 요약 잘림 방지).
"""

import os
import re
import anthropic

_DEFAULT_LIMIT = 180_000
_DEFAULT_COMPACT_TRIGGER = 0.85
_DEFAULT_COMPACT_MAX_TOKENS = 1500


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

def _compact_trigger() -> float:
    env = os.environ.get("ECC_COMPACT_TRIGGER")
    if env:
        try:
            v = float(env)
            if 0 < v < 1:
                return v
        except ValueError:
            pass
    return _DEFAULT_COMPACT_TRIGGER

def _compact_max_tokens() -> int:
    env = os.environ.get("ECC_COMPACT_MAX_TOKENS")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return _DEFAULT_COMPACT_MAX_TOKENS


def estimate_tokens(messages: list[dict]) -> int:
    """
    메시지 리스트의 토큰 수 추정.

    언어별 char-to-token 비율 적용:
      - ASCII (영어/코드): 4 chars ≈ 1 token
      - 한국어/일본어/중국어: 1~2 chars ≈ 1 token → 2 chars per token
      - 기타 유니코드: 3 chars ≈ 1 token (보수적 추정)

    실제 API 토크나이저와 완전히 일치하지 않으나
    압축 트리거 판단에 충분한 근사치를 제공.
    """
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            # tool_use / tool_result 블록: 각 블록의 텍스트 추출
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        total += _count_tokens(block.get("text", ""))
                    elif btype == "tool_use":
                        total += _count_tokens(str(block.get("input", "")))
                    elif btype == "tool_result":
                        total += _count_tokens(str(block.get("content", "")))
        else:
            total += _count_tokens(str(content))
    return total


def _count_tokens(text: str) -> int:
    """문자열의 토큰 수 추정 (혼합 언어 처리)."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    cjk_chars = sum(
        1 for ch in text
        if (0xAC00 <= ord(ch) <= 0xD7A3)   # 한글 음절
        or (0x1100 <= ord(ch) <= 0x11FF)   # 한글 자모
        or (0x4E00 <= ord(ch) <= 0x9FFF)   # CJK 통합 한자
        or (0x3040 <= ord(ch) <= 0x30FF)   # 히라가나/가타카나
    )
    other_chars = len(text) - ascii_chars - cjk_chars

    tokens = (
        ascii_chars // 4
        + cjk_chars  // 2
        + other_chars // 3
    )
    return max(tokens, len(text) // 4)  # 하한선: 기존 방식


def should_compact(messages: list[dict]) -> bool:
    return estimate_tokens(messages) > _context_limit() * _compact_trigger()


def compact(
    messages: list[dict],
    goal: str,
    todo_summary: str,
    client: anthropic.Anthropic,
    persistent_facts: str = "",
) -> list[dict]:
    """
    메시지 히스토리 압축.

    persistent_facts: ECCMemory.get_persistent_facts() 결과.
    물리적 제약, 하드웨어 사실 등 압축 후에도 반드시 보존해야 할 정보.
    """
    print("\n  📦 Compacting context......", flush=True)

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

    prompt = f"""The following is the conversation log from an embedded board automation task.

goal: {goal}

Conversation log:
{history_text}

From the above, extract and summarize concisely:
1. List of completed tasks
2. Discovered hardware info (device paths, IPs, parameters — keep specific values)
3. Failed approaches and reasons (important to prevent retry)
4. Current state (how far we got)
5. Remaining tasks

In 800 characters or less, facts only."""

    max_tokens = _compact_max_tokens()
    try:
        resp = client.messages.create(
            model=_compact_model(),
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = resp.content[0].text if resp.content else "(summary failed)"
    except Exception as e:
        summary = f"(Compacting context... Error: {e})"

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

    print(f"  ✅ {len(messages)} → {len(compacted)}  messages compacted", flush=True)
    return compacted