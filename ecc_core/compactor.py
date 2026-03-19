"""
ecc_core/compactor.py

Changelog:
  v2 — 한국어 토큰 추정 오류 수정.
  v3 — COMPACT_TRIGGER_RATIO, max_tokens env 오버라이드 추가.
  v4 — [Fix 5] Episode 중요도 기반 히스토리 선택.
  v5 — [Fix 3] _select_history_lines total_max 초과 시 중요 라인 우선 보호.
         기존: sorted(union)[-total_max:]으로 자르면 앞쪽 important_indices 소실 가능.
         수정: important_indices 먼저 확정 → 남은 슬롯을 recent 최신순으로 채움.
"""

import os
import re
import anthropic

_DEFAULT_LIMIT            = 180_000
_DEFAULT_COMPACT_TRIGGER  = 0.85
_DEFAULT_COMPACT_MAX_TOKENS = 1500

# [env override] 히스토리 선택 파라미터
# 기본값은 모듈 상수로 유지하되 런타임에 env로 오버라이드 가능
_HISTORY_RECENT_N    = 80
_HISTORY_IMPORTANT_N = 40
_HISTORY_TOTAL_MAX   = 120


def _history_recent_n() -> int:
    """최신 라인 유지 수. ECC_HISTORY_RECENT_N으로 오버라이드."""
    return int(os.environ.get("ECC_HISTORY_RECENT_N", _HISTORY_RECENT_N))

def _history_important_n() -> int:
    """중요도 상위 유지 수. ECC_HISTORY_IMPORTANT_N으로 오버라이드."""
    return int(os.environ.get("ECC_HISTORY_IMPORTANT_N", _HISTORY_IMPORTANT_N))

def _history_total_max() -> int:
    """히스토리 전체 상한. ECC_HISTORY_TOTAL_MAX으로 오버라이드."""
    return int(os.environ.get("ECC_HISTORY_TOTAL_MAX", _HISTORY_TOTAL_MAX))


def _compact_model() -> str:
    main = os.environ.get("ECC_MODEL", "claude-sonnet-4-6")
    return os.environ.get("ECC_COMPACT_MODEL", main)

def _context_limit() -> int:
    env = os.environ.get("ECC_CONTEXT_LIMIT")
    if env:
        try: return int(env)
        except ValueError: pass
    return _DEFAULT_LIMIT

def _compact_trigger() -> float:
    env = os.environ.get("ECC_COMPACT_TRIGGER")
    if env:
        try:
            v = float(env)
            if 0 < v < 1: return v
        except ValueError: pass
    return _DEFAULT_COMPACT_TRIGGER

def _compact_max_tokens() -> int:
    env = os.environ.get("ECC_COMPACT_MAX_TOKENS")
    if env:
        try: return int(env)
        except ValueError: pass
    return _DEFAULT_COMPACT_MAX_TOKENS


def estimate_tokens(messages: list[dict]) -> int:
    """메시지 리스트의 토큰 수 추정."""
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
    tokens = (ascii_chars // 4 + cjk_chars // 2 + other_chars // 3)
    return max(tokens, len(text) // 4)


def should_compact(messages: list[dict]) -> bool:
    return estimate_tokens(messages) > _context_limit() * _compact_trigger()


# ─────────────────────────────────────────────────────────────
# Episode 중요도 기반 히스토리 라인 선택
# ─────────────────────────────────────────────────────────────

def _importance_score_for_line(line: str) -> float:
    """텍스트 라인의 중요도 휴리스틱 점수 반환 (0.0 ~ 1.0)."""
    l = line.lower()
    if any(kw in l for kw in ("ssh_connect", "connected to", "discovered")):
        return 1.0
    if re.search(r'/dev/\w+', l):
        return 0.9
    if re.search(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', l):
        return 0.85
    if any(kw in l for kw in ("fail", "error", "rc=-1", "exception", "blocked", "safety")):
        return 0.8
    if any(kw in l for kw in ("pass", "verified", "remember", "constraint", "found", "ok]")):
        return 0.6
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
    all_lines:   list[str],
    recent_n:    int = _HISTORY_RECENT_N,
    important_n: int = _HISTORY_IMPORTANT_N,
    total_max:   int = _HISTORY_TOTAL_MAX,
) -> list[str]:
    """중요도 기반 + 최신 우선 혼합 선택.

    [Fix 3] total_max 초과 시 important_indices 우선 보호.

    알고리즘:
      1. recent_indices  = 마지막 recent_n 개 인덱스
      2. important_indices = 나머지 중 중요도 상위 important_n 개
      3. total_max 초과 시:
           - important_indices 전부 보존 (절대 제거 안 함)
           - 남은 슬롯 = total_max - len(important_indices)
           - recent_indices에서 가장 최신(인덱스 큰 것)부터 슬롯만큼만 포함

    기존 방식 [-total_max:] 문제:
      important_indices가 앞쪽(낮은 인덱스)에 있을 때
      sorted union을 뒤에서 자르면 통째로 제거될 수 있음.
    """
    if len(all_lines) <= total_max:
        return all_lines

    recent_indices    = set(range(max(0, len(all_lines) - recent_n), len(all_lines)))
    remaining         = [(i, l) for i, l in enumerate(all_lines) if i not in recent_indices]
    scored            = sorted(remaining, key=lambda x: _importance_score_for_line(x[1]), reverse=True)
    important_indices = {i for i, _ in scored[:important_n]}

    # [Fix 3] important를 전부 보존, 남은 슬롯을 recent 최신순으로 채움
    n_recent_slots   = max(0, total_max - len(important_indices))
    recent_sorted    = sorted(recent_indices, reverse=True)   # 최신(높은 인덱스) 우선
    selected_recent  = set(recent_sorted[:n_recent_slots])

    selected_indices = sorted(important_indices | selected_recent)
    return [all_lines[i] for i in selected_indices]


def compact(
    messages: list[dict],
    goal: str,
    todo_summary: str,
    client: anthropic.Anthropic,
    persistent_facts: str = "",
) -> list[dict]:
    """메시지 히스토리 압축."""
    print("\n  📦 Compacting context......", flush=True)

    history_lines: list[str] = []
    for m in messages[1:]:
        role    = m.get("role", "")
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
                    name   = block.get("name", "")
                    inp    = block.get("input", {})
                    detail = inp.get("command", inp.get("code", inp.get("path", str(inp))))[:120]
                    history_lines.append(f"[tool:{name}] {detail}")
                elif btype == "tool_result":
                    out = str(block.get("content", ""))[:200]
                    history_lines.append(f"[result] {out}")

    selected_lines = _select_history_lines(
        history_lines,
        recent_n    = _history_recent_n(),
        important_n = _history_important_n(),
        total_max   = _history_total_max(),
    )
    history_text   = "\n".join(selected_lines)

    _total   = len(history_lines)
    _kept    = len(selected_lines)
    _dropped = _total - _kept
    if _dropped > 0:
        print(
            f"  📊 History selection: {_kept}/{_total} lines kept "
            f"(recent≤{_history_recent_n()} + important≤{_history_important_n()}, "
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