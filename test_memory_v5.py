"""test_memory_v5.py - memory v4 + consolidation + tracer + goal_history 테스트"""
import sys, time
sys.path.insert(0, '/home/claude/repo')

from ecc_core.memory import ECCMemory, Episode

ADDR = "ubuntu@192.168.88.1:22"

def mk(mem):
    mem._conn_address = ADDR
    mem._checkpoint_path(ADDR).parent.mkdir(parents=True, exist_ok=True)
    return mem

# ── 1. Episode 중요도 점수 ───────────────────────────────────
ep_bash   = Episode.from_result("bash",    "ros2 topic list OK", ok=True,  turn=1)
ep_verify = Episode.from_result("verify",  "serial FAIL: port",  ok=False, turn=2)
ep_probe  = Episode.from_result("probe",   "found /dev/ttyACM0", ok=True,  turn=3)
ep_done   = Episode.from_result("done",    "task complete",       ok=True,  turn=4)

assert ep_bash.importance   < ep_verify.importance,  "실패가 성공보다 중요도 낮음"
assert ep_probe.importance  > ep_bash.importance,    "probe가 bash보다 중요도 낮음"
assert ep_done.importance   == 1.0,                  "done은 항상 1.0"
assert ep_verify.importance > 0.5,                   "실패 verify 중요도 낮음"
print(f"✅ 1. Episode 중요도: bash={ep_bash.importance:.2f} verify={ep_verify.importance:.2f} probe={ep_probe.importance:.2f} done={ep_done.importance:.2f}")

# ── 2. 인과 체인 ────────────────────────────────────────────
mem = mk(ECCMemory())
mem.record_episode("bash",   "ros2 topic list",      ok=True)
mem.record_episode("verify", "serial FAIL",          ok=False)
mem.record_episode("probe",  "found /dev/ttyACM0",   ok=True)

assert mem.episodic[1].caused_by != "",       "인과 링크 없음"
assert "bash" in mem.episodic[1].caused_by,   "caused_by가 bash를 가리키지 않음"
assert mem.episodic[2].caused_by != "",       "probe의 인과 링크 없음"
print(f"✅ 2. 인과 체인: verify.caused_by={mem.episodic[1].caused_by!r}")

# ── 3. Generative Agents 검색 공식 ──────────────────────────
mem2 = mk(ECCMemory())
mem2.working.turn = 10
for i, (tool, summary, ok) in enumerate([
    ("bash",   "echo hello",          True),
    ("probe",  "motor ttyACM0",       True),
    ("verify", "serial FAIL ttyACM0", False),
    ("bash",   "ros2 node list",      True),
]):
    ep = Episode.from_result(tool, summary, ok=ok, turn=i)
    mem2.episodic.append(ep)

results = mem2.retrieve_episodes("motor serial ttyACM0", top_k=2)
assert len(results) == 2
# motor/serial/ttyACM0 관련 에피소드가 상위에 와야 함
top_summaries = " ".join(e.summary for e in results)
assert any(k in top_summaries for k in ("motor", "serial", "ttyACM0"))
print(f"✅ 3. 검색 공식: top={[e.tool+':'+e.summary[:20] for e in results]}")

# ── 4. Semantic 쿼리 기반 필터 ──────────────────────────────
mem3 = mk(ECCMemory())
mem3.remember("hardware", "motor_topic", "/cmd_vel")
mem3.remember("hardware", "camera_dev",  "/dev/video0")
mem3.remember("constraints", "min_erpm", 2000)
mem3.remember("protocol",    "baud_rate", 115200)

result = mem3.semantic.query_relevant("motor speed erpm", top_k=4)
assert "motor_topic" in result or "min_erpm" in result, f"모터 관련 항목 미검색: {result}"
assert "camera_dev" not in result, "카메라 항목이 불필요하게 포함됨"
print(f"✅ 4. Semantic 쿼리 필터: motor 쿼리 -> motor_topic/min_erpm 포함")

# ── 5. to_system_context query 파라미터 ─────────────────────
ctx_with_q    = mem3.to_system_context(query="motor erpm")
ctx_without_q = mem3.to_system_context()
assert ctx_with_q    != "",  "쿼리 있는 컨텍스트 비어있음"
assert ctx_without_q != "",  "쿼리 없는 컨텍스트 비어있음"
print("✅ 5. to_system_context query 파라미터")

# ── 6. SSH 프로파일 캐싱 ────────────────────────────────────
mem4 = mk(ECCMemory())
mem4.update_connection("ubuntu@192.168.1.100:22")
profile = mem4.get_ssh_profile()
assert profile is not None,              "SSH 프로파일 없음"
assert profile["user"] == "ubuntu",      f"user 불일치: {profile}"
assert "192.168.1.100" in profile["host_port"], f"host 불일치: {profile}"
print(f"✅ 6. SSH 프로파일 캐싱: {profile}")

# ── 7. Tracer 비용 집계 ─────────────────────────────────────
from ecc_core.tracer import Tracer, _model_cost
import os; os.environ["ECC_TRACE"] = "0"  # 파일 쓰기 비활성화

t = Tracer(goal="test", enabled=True)
t.llm_call("claude-sonnet-4-6", 10000, 2000, 500)
t.llm_call("claude-opus-4-6",   5000,  1000, 800, escalated=True)
assert t._session_tokens_in  == 15000
assert t._session_tokens_out == 3000
assert t._llm_calls          == 2
assert t._escalated_calls    == 1
cost_sonnet = _model_cost("claude-sonnet-4-6", 10000, 2000)
cost_opus   = _model_cost("claude-opus-4-6",   5000,  1000)
assert abs(t._session_cost_usd - (cost_sonnet + cost_opus)) < 0.0001
tok_in, tok_out = t.get_token_totals()
assert tok_in  == 15000
assert tok_out == 3000
print(f"✅ 7. Tracer 비용 집계: total={tok_in+tok_out:,}tok cost=${t._session_cost_usd:.4f}")

# ── 8. Goal History ─────────────────────────────────────────
import tempfile, pathlib
from ecc_core import goal_history as gh
original_path = gh._HISTORY_PATH
gh._HISTORY_PATH = pathlib.Path(tempfile.mktemp(suffix=".jsonl"))

gh.record_goal("1m/s로 3초 주행", success=True,  turns=12, conn_address="ubuntu@192.168.1.1:22", tokens_in=50000, tokens_out=10000)
gh.record_goal("ROS2 셋업",       success=False, turns=7)
entries = gh.load_history()
assert len(entries) == 2
assert entries[0]["goal"] == "1m/s로 3초 주행"
assert entries[1]["success"] == False
formatted = gh.format_history(entries)
assert "✅" in formatted and "❌" in formatted
gh._HISTORY_PATH.unlink(missing_ok=True)
gh._HISTORY_PATH = original_path
print("✅ 8. Goal History 저장/로드/포맷")

# ── 9. 기존 checkpoint 테스트 호환 ─────────────────────────
mem5 = mk(ECCMemory())
mem5.working.goal = "checkpoint test"
mem5.working.turn = 5
mem5.episodic.append(Episode.from_result("bash", "test", ok=True, turn=5))
mem5.checkpoint_save()
mem6 = mk(ECCMemory())
ok = mem6.checkpoint_load()
assert ok and mem6.working.goal == "checkpoint test"
assert len(mem6.episodic) == 1 and mem6.episodic[0].tool == "bash"
mem5.checkpoint_clear()
print("✅ 9. 체크포인트 (v4 Episode 구조 호환)")

# ── 10. consolidation import ────────────────────────────────
from ecc_core.consolidation import consolidate_episodic, consolidate_skill
print("✅ 10. consolidation import OK")

print()
print("=" * 55)
print("memory v5 전체 검증 완료 (10/10 pass) ✅")
