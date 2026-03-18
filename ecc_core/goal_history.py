"""
ecc_core/goal_history.py — Goal 달성 이력

~/.ecc/history.jsonl 에 세션 요약 기록.
REPL에서 /history 명령으로 조회 가능.
"""

import json
import os
import time
from pathlib import Path


_HISTORY_PATH = Path("~/.ecc/history.jsonl").expanduser()


def record_goal(
    goal:         str,
    success:      bool,
    turns:        int,
    conn_address: str = "",
    summary:      str = "",
    tokens_in:    int = 0,
    tokens_out:   int = 0,
) -> None:
    """세션 종료 시 goal 이력 기록."""
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":           time.time(),
        "goal":         goal[:200],
        "success":      success,
        "turns":        turns,
        "conn_address": conn_address,
        "summary":      summary[:300],
        "tokens_in":    tokens_in,
        "tokens_out":   tokens_out,
    }
    try:
        with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_history(last_n: int = 20) -> list[dict]:
    """최근 N개 이력 로드."""
    if not _HISTORY_PATH.exists():
        return []
    try:
        lines = _HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        entries = [json.loads(l) for l in lines if l.strip()]
        return entries[-last_n:]
    except Exception:
        return []


def format_history(entries: list[dict]) -> str:
    """REPL /history 출력용 포맷."""
    if not entries:
        return "  (이력 없음)"
    lines = []
    for e in entries:
        ts      = time.strftime("%m/%d %H:%M", time.localtime(e["ts"]))
        icon    = "✅" if e["success"] else "❌"
        turns   = e.get("turns", "?")
        goal    = e["goal"][:55]
        conn    = e.get("conn_address", "")
        tok_in  = e.get("tokens_in", 0)
        tok_out = e.get("tokens_out", 0)
        tok_str = f" [{tok_in+tok_out:,}tok]" if tok_in + tok_out > 0 else ""
        lines.append(f"  {ts} {icon} [{turns}t]{tok_str} {goal!r}")
        if conn:
            lines.append(f"         @ {conn}")
    return "\n".join(lines)
