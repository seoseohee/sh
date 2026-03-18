"""
ecc_core/goal_history.py — Goal achievement history

Records session summaries to ~/.ecc/history.jsonl.
Accessible via /history command in REPL.
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
    """Record goal history on session end."""
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
    """Load last N history entries."""
    if not _HISTORY_PATH.exists():
        return []
    try:
        lines = _HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        entries = [json.loads(l) for l in lines if l.strip()]
        return entries[-last_n:]
    except Exception:
        return []


def format_history(entries: list[dict]) -> str:
    """Format for REPL /history output."""
    if not entries:
        return "  ((no history))"
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
