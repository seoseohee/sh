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
  v4 — [Fix 5] Episode 중요도 기반 히스토리 선택.
       기존: 마지막 120줄만 사용 → 초반 중요 발견(보드 IP, 첫 하드웨어 탐지)이 잘림.
       수정:
         1. 메시지 → 라인 변환 시 Episode 중요도 점수 병기
         2. "중요도 상위 N개 + 최근 M개" 혼합 선택으로 초반 핵심 정보 보존
         3. _select_history_lines() 함수로 선택 로직 분리
"""

import os
import re
import anthropic

_DEFAULT_LIMIT = 180_000
_DEFAULT_COMPACT_TRIGGER = 0.85
_DEFAULT_COMPACT_MAX_TOKENS = 1500

# [Fix 5] 히스토리 선택 파라미터
_HISTORY_RECENT_N   = 80   # 최신 N줄은 무조건 포함
_HISTORY_IMPORTANT_N = 40  # 중요도 상위 N줄 추가 포함 (중복 제거)
_HISTORY_TOTAL_MAX  = 120  # 전체 상한선 (기존과 동일)


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
    """
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
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
        if (0xAC00 <= ord(ch) <= 0xD7A3)
        or (0x1100 <= ord(ch) <= 0x11FF)
        or (0x4E00 <= ord(ch) <= 0x9FFF)
        or (0x3040 <= ord(ch) <= 0x30FF)
    )
    other_chars = len(text) - ascii_chars - cjk_chars

    tokens = (
        ascii_chars // 4
        + cjk_chars  // 2
        + other_chars // 3
    )
    return max(tokens, len(text) // 4)


def should_compact(messages: list[dict]) -> bool:
    return estimate_tokens(messages) > _context_limit() * _compact_trigger()


# ─────────────────────────────────────────────────────────────
# [Fix 5] Episode 중요도 기반 히스토리 라인 선택
# ─────────────────────────────────────────────────────────────

def _importance_score_for_line(line: str) -> float:
    """
    텍스트 라인의 중요도 휴리스틱 점수 반환 (0.0 ~ 1.0).

    메모리의 Episode.importance와 동일한 기준을 텍스트 기반으로 재현:
      - 연결/발견 이벤트: 높음 (ssh_connect, found, /dev/, IP 주소)
      - 실패 이벤트: 높음 (FAIL, error, rc=-1)
      - 검증 성공: 중간 (PASS, verified, connected)
      - 일반 bash 출력: 낮음
    """
    l = line.lower()

    # 최고 중요도: 연결 이벤트, 하드웨어 발견
    if any(kw in l for kw in ("ssh_connect", "connected to", "discovered")):
        return 1.0
    if re.search(r'/dev/\w+', l):
        return 0.9
    if re.search(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', l):
        return 0.85

    # 높은 중요도: 실패 이벤트
    if any(kw in l for kw in ("fail", "error", "rc=-1", "exception", "blocked", "safety")):
        return 0.8

    # 중간 중요도: 검증/발견 성공
    if any(kw in l for kw in ("pass", "verified", "remember", "constraint", "found", "ok]")):
        return 0.6

    # 툴 종류별 기본 중요도
    if l.startswith("[tool:remember") or l.startswith("[tool:probe"):
        return 0.7
    if l.startswith("[tool:verify"):
        return 0.65
    if l.startswith("[tool:ssh"):
        return 0.9
    if l.startswith("[tool:done"):
        return 1.0
    if l.startswith("[tool:bash"):
        return 0.3

    return 0.2


def _select_history_lines(
    all_lines: list[str],
    recent_n:    int = _HISTORY_RECENT_N,
    important_n: int = _HISTORY_IMPORTANT_N,
    total_max:   int = _HISTORY_TOTAL_MAX,
) -> list[str]:
    """
    [Fix 5] 중요도 기반 + 최신 우선 혼합 선택.

    전략:
      1. 마지막 recent_n 줄 → 최신 컨텍스트 보존
      2. 나머지 줄 중 중요도 상위 important_n 줄 → 초반 핵심 발견 보존
      3. 두 집합을 원래 순서(인덱스 기준)로 합쳐서 반환
      4. 전체 total_max 초과 시 잘라냄

    기존 behavior (마지막 120줄만):
      긴 세션에서 "IP 확인, 하드웨어 탐지" 등 초반 중요 이벤트 소실 가능

    개선 behavior:
      최신 80줄 + 중요도 높은 과거 40줄로 구성 → 핵심 발견 보존
    """
    if len(all_lines) <= total_max:
        return all_lines

    recent_indices  = set(range(max(0, len(all_lines) - recent_n), len(all_lines)))
    remaining_lines = [
        (i, line) for i, line in enumerate(all_lines)
        if i not in recent_indices
    ]

    # 중요도 점수로 정렬 후 상위 important_n 선택
    scored = sorted(remaining_lines, key=lambda x: _importance_score_for_line(x[1]), reverse=True)
    important_indices = {i for i, _ in scored[:important_n]}

    # 원래 순서 유지하여 합치기
    selected_indices = sorted(recent_indices | important_indices)

    # total_max 초과 시 뒤에서 자르기
    if len(selected_indices) > total_max:
        selected_indices = selected_indices[-total_max:]

    return [all_lines[i] for i in selected_indices]


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

    # [Fix 5] 단순 마지막 120줄 대신 중요도 기반 혼합 선택
    selected_lines = _select_history_lines(history_lines)
    history_text = "\n".join(selected_lines)

    _total   = len(history_lines)
    _kept    = len(selected_lines)
    _dropped = _total - _kept
    if _dropped > 0:
        print(
            f"  📊 History selection: {_kept}/{_total} lines kept "
            f"(recent={min(_HISTORY_RECENT_N, _total)} + "
            f"important={_kept - min(_HISTORY_RECENT_N, _total)}, "
            f"dropped={_dropped} low-importance lines)",
            flush=True,
        )

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