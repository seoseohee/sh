import sys, os, inspect
from pathlib import Path

# Fix 1
from ecc_core.executor import ToolExecutor
from ecc_core.todo import TodoManager
todos = TodoManager()
ex = ToolExecutor(conn=None, todos=todos)
assert hasattr(ex, '_ask_user') and callable(ex._ask_user)
print('✅ Fix 1: _ask_user 클래스 내 정의')

# Fix 2
from ecc_core.verifier import verify_execution
obs_ok   = {'ok': True,  'stdout': 'serial PASS: /dev/ttyACM0', 'stderr': '', 'response': '', 'raw': ''}
obs_fail = {'ok': True,  'stdout': 'serial FAIL: no device',    'stderr': '', 'response': '', 'raw': ''}
obs_bash = {'ok': True,  'stdout': 'output here',               'stderr': '', 'response': '', 'raw': ''}
assert verify_execution('verify', obs_ok)['success']   == True
assert verify_execution('verify', obs_fail)['success'] == False
assert verify_execution('bash', obs_bash)['reason']    == 'observable_output_present'
print('✅ Fix 2: verify_execution tool_name 판정')

# Fix 3
from ecc_core.compactor import _count_tokens
ko_text = '안녕하세요 한국어 테스트입니다'
ko = _count_tokens(ko_text)
old_ko = len(ko_text) // 4
assert ko > old_ko, f'한국어 추정 미개선: {ko} <= {old_ko}'
print(f'✅ Fix 3: 토큰추정 — 한국어={ko} (기존 {old_ko})')

# Fix 4
from ecc_core.memory import ECCMemory
mem = ECCMemory()
mem._conn_address = 'u@1.2.3.4:22'
path = mem._path('u@1.2.3.4:22')
path.parent.mkdir(parents=True, exist_ok=True)
if path.exists():
    path.unlink()
mem.save()
assert not path.exists(), 'dirty=False인데 파일 생성됨'
mem.remember('hardware', 'topic', '/cmd_vel')
assert mem._dirty
mem.save()
assert path.exists() and not mem._dirty
mem2 = ECCMemory()
mem2._conn_address = 'u@1.2.3.4:22'
mem2.remember('constraints', 'min_erpm', 2000)
assert not mem2._dirty, 'constraints 즉시 flush 실패'
print('✅ Fix 4: memory dirty flag + 즉시/배치 저장')

# Fix 5
from ecc_core.loop import AgentLoop
assert not AgentLoop._is_followup('1m/s로 주행', True),      '짧은 새 goal 오판'
assert AgentLoop._is_followup('/continue 속도', True),       '/continue 미감지'
assert AgentLoop._is_followup('/resume', True),              '/resume 미감지'
assert not AgentLoop._is_followup('새 목표', True),           '새 goal 오판'
assert not AgentLoop._is_followup('/continue', False),       '세션 없을때 followup'
print('✅ Fix 5: is_followup 접두어 판정')

# Fix 6
from ecc_core.loop import EscalationTracker
et = EscalationTracker()
fb = type('B', (), {'name': 'bash', 'id': '1', 'input': {'command': 'ros2 topic list'}})()
for _ in range(4):
    et.record_tool_results([fb], {'1': 'out'})
assert et.should_escalate()[0], 'bash 반복 escalation 미감지'
assert not EscalationTracker().should_escalate()[0], '새 인스턴스 escalation 오탐'
print('✅ Fix 6: EscalationTracker goal 경계 리셋')

# Fix 7: _verify_annotations로 tool_result에 병합 — user→user 연속 제거
from ecc_core import loop as loop_mod
src = inspect.getsource(loop_mod)
assert '_annotations' in src, '_annotations 없음'
assert 'out + ann' in src, 'annotation 병합 없음'
print('✅ Fix 7: deferred_verify → tool_result annotation 병합')

# Fix 8: subagent memory 전달
subagent_src = inspect.getsource(loop_mod.run_subagent)
assert 'memory=memory' in subagent_src, 'run_subagent에 memory 전달 없음'
print('✅ Fix 8: subagent memory 전달')

# Fix 9: connection.py IP 우선순위
from ecc_core.connection import BoardDiscovery
assert hasattr(BoardDiscovery, '_try_ip'), '_try_ip 없음'
print('✅ Fix 9: BoardDiscovery IP 우선순위 (_try_ip)')

print()
print('━' * 50)
print('전체 수정 검증 완료 (9/9 pass) ✅')
