"""
ecc_core/compactor.py

Environment variables:
  ECC_COMPACT_MODEL   Compression model (default: ECC_MODEL, fallback: claude-sonnet-4-6)
  ECC_CONTEXT_LIMIT   Context token limit (default: auto per model)

Changelog:
  v2 — Fixed Korean token estimation error.
       Old chars // 4 was English-based (1 token ≈ 4 chars).
       Korean is 1 token ≈ 1~2 chars, so estimate was less than half actual.
       Result: compaction triggered too late → BadRequestError.
       Fix: estimate_tokens() with per-character-type weighting.
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
    Estimate token count for a message list.

    FIX v2: Apply per-language char-to-token ratios.
      - ASCII (English/code): 4 chars ≈ 1 token
      - Korean/Japanese/Chinese: 1~2 chars ≈ 1 token → use 2 chars per token
      - Other Unicode: 3 chars ≈ 1 token (conservative estimate)

    Not perfectly aligned with the actual API tokenizer,
    but provides a sufficient approximation for compaction trigger decisions.
    """
    total = 0
    for m in messages:
        c = str(m.get("content", ""))
        total += _count_tokens(c)
    return total


def _count_tokens(text: str) -> int:
    """Estimate token count for a string (handles mixed languages)."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    # CJK Unified Ideographs + Hangul Syllables + Katakana + Hiragana
    cjk_chars = sum(
        1 for ch in text
        if (0xAC00 <= ord(ch) <= 0xD7A3)   # Hangul syllables
        or (0x1100 <= ord(ch) <= 0x11FF)   # Hangul jamo
        or (0x4E00 <= ord(ch) <= 0x9FFF)   # CJK unified ideographs
        or (0x3040 <= ord(ch) <= 0x30FF)   # Hiragana/Katakana
    )
    other_chars = len(text) - ascii_chars - cjk_chars

    tokens = (
        ascii_chars // 4       # English/code
        + cjk_chars // 2       # Korean/Chinese/Japanese
        + other_chars // 3     # Other Unicode
    )
    return max(tokens, len(text) // 4)  # Lower bound: old method


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
    Compress message history.

    persistent_facts: result of ECCMemory.get_persistent_facts().
    Physical constraints, hardware facts etc. that must survive compression.
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

In 600 characters or less, facts only."""

    try:
        resp = client.messages.create(
            model=_compact_model(),
            max_tokens=800,
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
