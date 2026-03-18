"""test_checkpoint.py — Working + Episodic 체크포인트 기능 테스트."""
import sys, time
sys.path.insert(0, '/home/claude/repo')

from ecc_core.memory import ECCMemory, Episode

ADDR = "ubuntu@192.168.99.1:22"

def clean(mem):
    mem.checkpoint_clear()
    p = mem._checkpoint_path(ADDR)
    assert not p.exists(), "clear 후 파일 잔존"

# ── 1. 기본 save / load ──────────────────────────────────
mem = ECCMemory()
mem._conn_address = ADDR
mem._checkpoint_path(ADDR).parent.mkdir(parents=True, exist_ok=True)

# 초기엔 체크포인트 없음
assert not mem.checkpoint_exists(), "초기 체크포인트 존재"

# working memory 설정
mem.working.goal         = "1m/s로 3초 주행"
mem.working.current_step = "모터 속도 확인 중"
mem.working.last_action  = "bash"
mem.working.last_result  = "[ok] speed=1.02"
mem.working.turn         = 7

# episodic 추가
mem.episodic.append(Episode(ts=time.time(), tool="bash",   summary="ros2 topic list OK", ok=True))
mem.episodic.append(Episode(ts=time.time(), tool="verify", summary="serial FAIL",         ok=False))
mem.episodic.append(Episode(ts=time.time(), tool="script", summary="drive command sent",  ok=True))

mem.checkpoint_save()
assert mem.checkpoint_exists(), "save 후 파일 없음"
print("✅ 1. checkpoint_save()")

# ── 2. 새 인스턴스에서 복원 ──────────────────────────────
mem2 = ECCMemory()
mem2._conn_address = ADDR

restored = mem2.checkpoint_load()
assert restored, "checkpoint_load() False 반환"

assert mem2.working.goal         == "1m/s로 3초 주행",    f"goal 불일치: {mem2.working.goal}"
assert mem2.working.current_step == "모터 속도 확인 중",   f"step 불일치"
assert mem2.working.last_action  == "bash",               f"last_action 불일치"
assert mem2.working.last_result  == "[ok] speed=1.02",    f"last_result 불일치"
assert mem2.working.turn         == 7,                    f"turn 불일치: {mem2.working.turn}"
assert len(mem2.episodic)        == 3,                    f"episodic 길이 불일치: {len(mem2.episodic)}"
assert mem2.episodic[1].ok       == False,                "episodic[1].ok 불일치"
assert mem2.episodic[1].summary  == "serial FAIL",        "episodic summary 불일치"
print("✅ 2. checkpoint_load() — Working + Episodic 완전 복원")

# ── 3. Episodic 50개 cap ─────────────────────────────────
mem3 = ECCMemory()
mem3._conn_address = ADDR
for i in range(80):
    mem3.episodic.append(Episode(ts=time.time(), tool="bash", summary=f"cmd {i}", ok=True))
mem3.checkpoint_save()

mem4 = ECCMemory()
mem4._conn_address = ADDR
mem4.checkpoint_load()
assert len(mem4.episodic) == 50, f"cap 50 미작동: {len(mem4.episodic)}"
print("✅ 3. Episodic 50개 cap")

# ── 4. clear ─────────────────────────────────────────────
mem3.checkpoint_clear()
assert not mem3.checkpoint_exists(), "clear 후 파일 잔존"

mem5 = ECCMemory()
mem5._conn_address = ADDR
result = mem5.checkpoint_load()
assert not result, "clear 후 load가 True 반환"
print("✅ 4. checkpoint_clear()")

# ── 5. 연결 주소 없을 때 graceful ────────────────────────
mem6 = ECCMemory()  # _conn_address = ""
mem6.checkpoint_save()   # 조용히 무시
mem6.checkpoint_clear()  # 조용히 무시
assert not mem6.checkpoint_load(), "주소 없을 때 load True"
print("✅ 5. conn_address 없을 때 graceful no-op")

# ── 6. Semantic과 분리 확인 ──────────────────────────────
mem7 = ECCMemory()
mem7._conn_address = ADDR
mem7.remember("hardware", "motor_topic", "/cmd_vel")
mem7.checkpoint_save()

# checkpoint 파일과 semantic 파일이 다른지
cp_path  = mem7._checkpoint_path(ADDR)
sem_path = mem7._path(ADDR)
assert cp_path != sem_path, "체크포인트와 Semantic이 같은 파일"
assert ".checkpoint.json" in str(cp_path), "체크포인트 파일명 오류"
print("✅ 6. Semantic 파일과 분리")

# 정리
for addr in [ADDR]:
    m = ECCMemory(); m._conn_address = addr
    m.checkpoint_clear()
    p = m._path(addr)
    if p.exists(): p.unlink()

print()
print("━" * 50)
print("체크포인트 전체 검증 완료 (6/6 pass) ✅")
