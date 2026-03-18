"""ecc_core/tracer.py - JSONL agent tracer + session cost monitoring

가격 기준: 2025-05 Anthropic 공시가
  ECC_COST_<MODEL>_IN / ECC_COST_<MODEL>_OUT (USD per 1M tokens) 으로 오버라이드 가능.
  예) export ECC_COST_SONNET_IN=3.0 ECC_COST_SONNET_OUT=15.0
"""

import json
import re
import time
import os
from pathlib import Path

# 기본 가격 (USD per 1M tokens, 2025-05 기준)
# 가격이 변경되면 env 변수로 오버라이드하거나 아래 값을 수정.
_DEFAULT_COST: dict[str, tuple[float, float]] = {
    "sonnet": (3.0,  15.0),
    "opus":   (15.0, 75.0),
    "haiku":  (0.25, 1.25),
}


def _cost_per_1m(model: str) -> tuple[float, float]:
    """모델별 (input, output) USD/1M 토큰 반환. env 오버라이드 우선."""
    key = next((k for k in _DEFAULT_COST if k in model.lower()), "sonnet")
    env_in  = os.environ.get(f"ECC_COST_{key.upper()}_IN")
    env_out = os.environ.get(f"ECC_COST_{key.upper()}_OUT")
    base_in, base_out = _DEFAULT_COST[key]
    try:
        cin  = float(env_in)  if env_in  else base_in
        cout = float(env_out) if env_out else base_out
    except ValueError:
        cin, cout = base_in, base_out
    return cin, cout


def _model_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    cin, cout = _cost_per_1m(model)
    return (tokens_in * cin + tokens_out * cout) / 1_000_000


class Tracer:
    """JSONL-based agent tracer. All methods are no-ops when enabled=False."""

    def __init__(self, goal: str, enabled: bool = True):
        self.enabled = enabled
        self._path: Path | None = None
        self._session_tokens_in:  int   = 0
        self._session_tokens_out: int   = 0
        self._session_cost_usd:   float = 0.0
        self._llm_calls:          int   = 0
        self._escalated_calls:    int   = 0

        if not enabled:
            return
        if os.environ.get("ECC_TRACE", "1") == "0":
            self.enabled = False
            return

        ts   = int(time.time())
        slug = re.sub(r"[^\w\uAC00-\uD7A3]", "_", goal[:28]).strip("_") or "session"
        self._path = Path(f"~/.ecc/traces/{ts}_{slug}.jsonl").expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._write({"event": "session_start", "goal": goal[:200],
                     "ts": ts, "pid": os.getpid()})

    def llm_call(self, model, tokens_in, tokens_out, duration_ms, escalated=False):
        self._session_tokens_in  += tokens_in
        self._session_tokens_out += tokens_out
        self._session_cost_usd   += _model_cost(model, tokens_in, tokens_out)
        self._llm_calls          += 1
        if escalated:
            self._escalated_calls += 1
        self._write({
            "event": "llm_call", "model": model,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_tokens": tokens_in + tokens_out,
            "duration_ms": duration_ms, "escalated": escalated,
            "ts": time.time(),
        })

    def tool_use(self, name, inp_summary, out_summary, ok, duration_ms=0):
        self._write({
            "event": "tool_use", "tool": name,
            "input": inp_summary[:200], "output": out_summary[:200],
            "ok": ok, "duration_ms": duration_ms, "ts": time.time(),
        })

    def reflection(self, decision, text):
        self._write({"event": "reflection", "decision": decision,
                     "text": text[:300], "ts": time.time()})

    def note(self, message):
        self._write({"event": "note", "message": message[:300], "ts": time.time()})

    def session_end(self, success: bool, summary: str = "") -> dict:
        stats = {
            "tokens_in":       self._session_tokens_in,
            "tokens_out":      self._session_tokens_out,
            "tokens_total":    self._session_tokens_in + self._session_tokens_out,
            "cost_usd":        round(self._session_cost_usd, 4),
            "llm_calls":       self._llm_calls,
            "escalated_calls": self._escalated_calls,
        }
        self._write({
            "event": "session_end", "success": success,
            "summary": summary[:200], "ts": time.time(), **stats,
        })
        if self._llm_calls > 0:
            tok   = stats["tokens_total"]
            cost  = stats["cost_usd"]
            calls = stats["llm_calls"]
            esc   = stats["escalated_calls"]
            print(
                f"\n  {'='*60}\n"
                f"  Session cost: {tok:,} tokens | ~${cost:.4f} USD"
                f" | LLM {calls} calls (escalated {esc})",
                flush=True,
            )
        return stats

    def get_token_totals(self) -> tuple[int, int]:
        return self._session_tokens_in, self._session_tokens_out

    def _write(self, obj: dict) -> None:
        if not self.enabled or self._path is None:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            pass