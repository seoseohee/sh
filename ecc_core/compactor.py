"""
ecc_core/compactor.py

환경변수:
  ECC_COMPACT_MODEL   압축용 모델 (기본: ECC_MODEL, 없으면 claude-sonnet-4-6)
  ECC_CONTEXT_LIMIT   컨텍스트 토큰 한계 (기본: 모델별 자동 설정)
"""

import os
import anthropic

COMPACT_TRIGGER_RATIO = 0.85

_DEFAULT_LIMIT = 180_000  # 실제 200k 중 안전 마진


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
    total = 0
    for m in messages:
        c = m.get("content", "")
        total += len(str(c)) // 4
    return total


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
    압축 요약 앞에 배치해서 에이전트가 재발견하지 않도록 한다.
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

    # persistent_facts가 있으면 압축 후에도 맨 앞에 배치
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
