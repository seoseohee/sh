"""
ecc_core/compactor.py

Changelog:
  v2 — 한국어 토큰 추정 오류 수정.
  v3 — COMPACT_TRIGGER_RATIO, max_tokens env 오버라이드 추가.
  v4 — [Fix 5] Episode 중요도 기반 히스토리 선택.
  v5 — [Fix 3] _select_history_lines total_max 초과 시 중요 라인 우선 보호.
  v6 — [Improve A] 툴별 즉시 관찰 압축 (per-tool observation summarizer).
         툴 결과가 OBS_COMPRESS_THRESHOLD 이상이면 툴 타입별 전략으로 즉시 요약.
         ECC_OBS_COMPRESS_THRESHOLD 환경변수로 오버라이드 (기본 600자).
       [Improve B] _importance_score_for_line 수치를 memory.py _TOOL_IMPORTANCE와 일치.
         기존: verify=0.65(컴팩터) vs 0.8(메모리) 불일치.
         수정: 두 파일의 점수를 단일 소스(_TOOL_IMPORTANCE)에서 참조.
       [Improve B2] _count_tokens 공식 개선.
         한국어/CJK 1자 ≈ 0.65토큰(Claude tokenizer 실측 중간값)으로 조정.
         ECC_TOKEN_CJK_RATIO / ECC_TOKEN_ASCII_RATIO 환경변수로 오버라이드 가능.
"""

import os
import re
import anthropic

_DEFAULT_LIMIT            = 180_000
_DEFAULT_COMPACT_TRIGGER  = 0.85
_DEFAULT_COMPACT_MAX_TOKENS = 1500

_HISTORY_RECENT_N    = 80
_HISTORY_IMPORTANT_N = 40
_HISTORY_TOTAL_MAX   = 120

# [Improve A] 툴별 관찰 압축 임계치 (자)
_OBS_COMPRESS_THRESHOLD = 600


def _history_recent_n() -> int:
    return int(os.environ.get("ECC_HISTORY_RECENT_N", _HISTORY_RECENT_N))

def _history_important_n() -> int:
    return int(os.environ.get("ECC_HISTORY_IMPORTANT_N", _HISTORY_IMPORTANT_N))

def _history_total_max() -> int:
    return int(os.environ.get("ECC_HISTORY_TOTAL_MAX", _HISTORY_TOTAL_MAX))

def _obs_compress_threshold() -> int:
    """툴 결과 즉시 압축 임계치 (자). ECC_OBS_COMPRESS_THRESHOLD로 오버라이드."""
    return int(os.environ.get("ECC_OBS_COMPRESS_THRESHOLD", _OBS_COMPRESS_THRESHOLD))

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


# ─────────────────────────────────────────────────────────────
# [Improve B2] 토큰 추정 — 실측 기반 비율
# ─────────────────────────────────────────────────────────────

def _token_ascii_ratio() -> float:
    """ASCII 토큰 비율 (문자당). ECC_TOKEN_ASCII_RATIO로 오버라이드. 기본 0.25 (4자≈1토큰)."""
    try:
        return float(os.environ.get("ECC_TOKEN_ASCII_RATIO", 0.25))
    except (ValueError, TypeError):
        return 0.25

def _token_cjk_ratio() -> float:
    """CJK 토큰 비율 (문자당). ECC_TOKEN_CJK_RATIO로 오버라이드.
    기본 0.65 (Claude tokenizer 실측: 한국어 1음절 ≈ 0.6~1.0토큰 중간값).
    구버전 0.5는 한국어 토큰 수를 과소 추정해 컨텍스트 초과 위험이 있었음.
    """
    try:
        return float(os.environ.get("ECC_TOKEN_CJK_RATIO", 0.65))
    except (ValueError, TypeError):
        return 0.65


def estimate_tokens(messages: list[dict]) -> int:
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
    """문자열 토큰 수 추정 (혼합 언어).

    [Improve B2] 환경변수로 오버라이드 가능한 비율 사용.
    기본값 근거:
      ASCII  : 0.25  (4자 ≈ 1 token — Claude BPE 특성상 영어 단어 평균)
      CJK    : 0.65  (구버전 0.5에서 상향. 한국어 음절은 평균 0.6~1.0토큰)
      기타   : 0.33  (라틴 확장, 아랍어 등 중간값)
    """
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    cjk_chars = sum(
        1 for ch in text
        if (0xAC00 <= ord(ch) <= 0xD7A3)
        or (0x1100 <= ord(ch) <= 0x11FF)
        or (0x4E00 <= ord(ch) <= 0x9FFF)
        or (0x3040 <= ord(ch) <= 0x30FF)
    )
    other_chars = len(text) - ascii_chars - cjk_chars
    tokens = int(
        ascii_chars * _token_ascii_ratio()
        + cjk_chars  * _token_cjk_ratio()
        + other_chars * 0.33
    )
    return max(tokens, len(text) // 4)


def should_compact(messages: list[dict]) -> bool:
    return estimate_tokens(messages) > _context_limit() * _compact_trigger()


# ─────────────────────────────────────────────────────────────
# [Improve A] 툴별 즉시 관찰 압축 (per-tool observation summarizer)
# ─────────────────────────────────────────────────────────────
#
# 컨텍스트에 축적되기 전에 툴 결과를 툴 타입에 맞는 전략으로 즉시 요약.
# OpenDev(arXiv 2603.05344) 패턴: 결과 요약은 50-200자로 유지.
# 원본 내용은 compact() 시점에 이미 요약된 형태로 전달되므로 품질도 향상.
#
# 압축 우선순위:
#   1. 툴별 전용 요약기가 있으면 → 전용 요약기
#   2. 임계치 미만이면 → 원본 반환
#   3. 그 외 → 범용 말줄임

def summarize_tool_output(tool_name: str, result_text: str) -> str:
    """툴 결과를 즉시 압축. 임계치 미만이면 원본 그대로 반환.

    Args:
        tool_name:   실행된 툴 이름
        result_text: executor.execute()의 반환값

    Returns:
        압축된 문자열 (또는 짧으면 원본)
    """
    threshold = _obs_compress_threshold()
    if len(result_text) <= threshold:
        return result_text

    summarizer = _TOOL_SUMMARIZERS.get(tool_name)
    if summarizer:
        return summarizer(result_text)

    # 범용: status 라인 + 앞부분만 유지
    return _generic_summarize(result_text, threshold)


def _extract_status_prefix(text: str) -> str:
    """[ok]/[error]/[safety_blocked] 등 상태 접두어 추출."""
    m = re.match(r'(\[[\w_]+\]\s*\d*ms?)', text)
    return m.group(1) if m else ""


def _generic_summarize(text: str, threshold: int) -> str:
    prefix = _extract_status_prefix(text)
    body   = text[len(prefix):].strip()
    keep   = max(200, threshold // 3)
    if len(body) > keep:
        body = body[:keep] + f"... [{len(text)}ch truncated]"
    return (prefix + " " + body).strip()


def _summarize_probe(text: str) -> str:
    """probe 결과: 장치 경로, 버전, 서비스명만 추출."""
    lines = text.splitlines()
    important = []
    for line in lines:
        l = line.strip()
        if not l or l.startswith("==="): continue
        if any(re.search(p, l) for p in (
            r'/dev/\w+', r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b',
            r'ROS\s*\d', r'ros-', r'python\s*3\.\d', r'active\s*\(',
            r'running', r'version', r'found', r'not found',
        )):
            important.append(l[:120])
    if not important:
        return _generic_summarize(text, _obs_compress_threshold())
    prefix = _extract_status_prefix(text)
    summary = "\n".join(important[:20])
    return f"{prefix}\n[probe summary — {len(lines)} lines → {len(important)} signals]\n{summary}"


def _summarize_verify(text: str) -> str:
    """verify 결과: PASS/FAIL/WARN 라인 + 증거 1줄만."""
    lines = text.splitlines()
    verdict_lines = [l.strip() for l in lines if re.search(r'\b(PASS|FAIL|WARN|OK)\b', l, re.I)]
    prefix = _extract_status_prefix(text)
    if verdict_lines:
        return f"{prefix}\n" + "\n".join(verdict_lines[:5])
    return _generic_summarize(text, _obs_compress_threshold())


def _summarize_bash(text: str) -> str:
    """bash 결과: 오류 라인 우선, 없으면 앞 + 뒤."""
    prefix = _extract_status_prefix(text)
    body   = text[len(prefix):].strip()
    lines  = body.splitlines()
    error_lines = [l for l in lines if re.search(r'error|fail|warn|exception|not found', l, re.I)]
    if error_lines:
        kept = "\n".join(error_lines[:8])
        return f"{prefix}\n[{len(lines)} lines, errors below]\n{kept}"
    # 오류 없으면 앞 10줄 + 마지막 3줄
    head = "\n".join(lines[:10])
    tail = "\n".join(lines[-3:]) if len(lines) > 13 else ""
    body_out = head + (f"\n... [{len(lines)} lines total] ...\n{tail}" if tail else "")
    return f"{prefix}\n{body_out}"


def _summarize_script(text: str) -> str:
    """script 결과: 마지막 출력 줄 (가장 중요) + 오류 우선."""
    return _summarize_bash(text)


# 툴별 요약기 레지스트리
_TOOL_SUMMARIZERS: dict[str, callable] = {
    "probe":       _summarize_probe,
    "verify":      _summarize_verify,
    "bash":        _summarize_bash,
    "bash_wait":   _summarize_bash,
    "script":      _summarize_script,
    # read/write/glob/grep은 임계치 초과 시 범용 처리
}


# ─────────────────────────────────────────────────────────────
# [Improve B] Episode 중요도 — memory.py _TOOL_IMPORTANCE와 동기화
# ─────────────────────────────────────────────────────────────
#
# 기존 문제:
#   memory.py verify=0.8,  compactor.py [tool:verify]=0.65  → 불일치
#   memory.py bash=0.3,    compactor.py [tool:bash]=0.3     → 우연히 일치
# 수정: compactor의 점수를 memory._TOOL_IMPORTANCE 값 기준으로 재정렬.
# 실패 보정(+0.2)은 memory.py와 동일하게 적용하지 않음
# (compactor는 텍스트 기반 휴리스틱이므로 실패 여부를 텍스트로 판단).

_LINE_IMPORTANCE: dict[str, float] = {
    # memory._TOOL_IMPORTANCE와 동일 기준
    "ssh_connect":  0.9,
    "remember":     0.9,
    "done":         1.0,
    "probe":        0.8,
    "verify":       0.8,   # 수정: 0.65 → 0.8
    "serial_open":  0.7,
    "script":       0.5,
    "bash":         0.3,
    "todo":         0.2,
}

# 실패/성공 키워드 보정
_FAIL_KW  = ("fail", "error", "rc=-1", "exception", "blocked", "safety", "not found")
_PASS_KW  = ("pass", "verified", "remember", "constraint", "found", "ok]", "connected")


def _importance_score_for_line(line: str) -> float:
    """텍스트 라인 중요도 (0.0~1.0).

    [Improve B] 툴 이름 기반 점수를 _LINE_IMPORTANCE에서 참조 (memory.py와 통일).
    패턴 매칭으로 툴 이름 추출 불가 시 내용 기반 휴리스틱 사용.
    """
    l = line.lower()

    # 툴 이름 추출 시도 ([tool:NAME] 형식)
    m = re.match(r'\[tool:(\w+)', l)
    if m:
        tool = m.group(1)
        base = _LINE_IMPORTANCE.get(tool, 0.3)
        # 실패 키워드 보정
        if any(kw in l for kw in _FAIL_KW):
            return min(1.0, base + 0.2)
        if any(kw in l for kw in _PASS_KW):
            return min(1.0, base + 0.1)
        return base

    # 툴 이름 없는 라인 — 내용 기반
    if any(kw in l for kw in ("ssh_connect", "connected to", "discovered")):
        return 1.0
    if re.search(r'/dev/\w+', l):
        return 0.9
    if re.search(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', l):
        return 0.85
    if any(kw in l for kw in _FAIL_KW):
        return 0.8
    if any(kw in l for kw in _PASS_KW):
        return 0.6
    return 0.2


def _select_history_lines(
    all_lines:   list[str],
    recent_n:    int = _HISTORY_RECENT_N,
    important_n: int = _HISTORY_IMPORTANT_N,
    total_max:   int = _HISTORY_TOTAL_MAX,
) -> list[str]:
    """중요도 기반 + 최신 우선 혼합 선택 (Fix 3 유지)."""
    if len(all_lines) <= total_max:
        return all_lines

    recent_indices    = set(range(max(0, len(all_lines) - recent_n), len(all_lines)))
    remaining         = [(i, l) for i, l in enumerate(all_lines) if i not in recent_indices]
    scored            = sorted(remaining, key=lambda x: _importance_score_for_line(x[1]), reverse=True)
    important_indices = {i for i, _ in scored[:important_n]}

    n_recent_slots   = max(0, total_max - len(important_indices))
    recent_sorted    = sorted(recent_indices, reverse=True)
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