"""
ecc_core/tracer.py

경량 JSONL 에이전트 트레이서.
OpenTelemetry GenAI 시맨틱 컨벤션 기반 설계 (2026 표준).

각 이벤트를 ~/.ecc/traces/<timestamp>_<goal_slug>.jsonl 에 기록.
외부 의존성 없음 — 표준 라이브러리만 사용.
트레이싱 실패는 조용히 무시 (best-effort).

이벤트 종류:
  session_start  — 세션 시작
  llm_call       — API 호출 (model, tokens, duration)
  tool_use       — 도구 실행 (name, input, output, ok, duration)
  reflection     — Reflexion 생성 (decision, text)
  note           — 기타 시스템 이벤트 (compact, escalation 등)
  session_end    — 세션 종료 (success, summary)

분석 예시:
  # 세션별 총 토큰 비용
  jq 'select(.event=="llm_call") | .tokens_in + .tokens_out' trace.jsonl | paste -sd+ | bc

  # 실패한 도구 목록
  jq 'select(.event=="tool_use" and .ok==false) | .tool + ": " + .output' trace.jsonl

  # Escalation 빈도
  jq 'select(.event=="llm_call" and .escalated==true)' trace.jsonl | wc -l
"""

import json
import re
import time
import os
from pathlib import Path


class Tracer:
    """
    JSONL 기반 에이전트 트레이서.
    enabled=False이면 모든 메서드가 no-op.
    """

    def __init__(self, goal: str, enabled: bool = True):
        self.enabled = enabled
        self._path: Path | None = None

        if not enabled:
            return

        # ECC_TRACE=0 이면 비활성화
        if os.environ.get("ECC_TRACE", "1") == "0":
            self.enabled = False
            return

        ts = int(time.time())
        slug = re.sub(r"[^\w가-힣]", "_", goal[:28]).strip("_") or "session"
        self._path = Path(f"~/.ecc/traces/{ts}_{slug}.jsonl").expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._write({
            "event": "session_start",
            "goal": goal[:200],
            "ts": ts,
            "pid": os.getpid(),
        })

    # ── 이벤트 기록 메서드 ─────────────────────────────────────

    def llm_call(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        duration_ms: int,
        escalated: bool = False,
    ) -> None:
        self._write({
            "event": "llm_call",
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_tokens": tokens_in + tokens_out,
            "duration_ms": duration_ms,
            "escalated": escalated,
            "ts": time.time(),
        })

    def tool_use(
        self,
        name: str,
        inp_summary: str,
        out_summary: str,
        ok: bool,
        duration_ms: int = 0,
    ) -> None:
        self._write({
            "event": "tool_use",
            "tool": name,
            "input": inp_summary[:200],
            "output": out_summary[:200],
            "ok": ok,
            "duration_ms": duration_ms,
            "ts": time.time(),
        })

    def reflection(self, decision: str, text: str) -> None:
        self._write({
            "event": "reflection",
            "decision": decision,
            "text": text[:300],
            "ts": time.time(),
        })

    def note(self, message: str) -> None:
        self._write({
            "event": "note",
            "message": message[:300],
            "ts": time.time(),
        })

    def session_end(self, success: bool, summary: str = "") -> None:
        self._write({
            "event": "session_end",
            "success": success,
            "summary": summary[:200],
            "ts": time.time(),
        })

    # ── 내부 ───────────────────────────────────────────────────

    def _write(self, obj: dict) -> None:
        if not self.enabled or self._path is None:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 트레이싱 실패는 조용히 무시
