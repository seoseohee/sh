import argparse
import os
import sys
import textwrap
from .loop import AgentLoop
from .goal_history import load_history, format_history

def main():
    parser = argparse.ArgumentParser(
        prog="ecc",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        예시:
          python ecc.py "시스템 상태 확인"
          python ecc.py "1m/s로 3초 주행"
          python ecc.py --host 192.168.1.100 "차량 주행"
          python ecc.py   # REPL 모드
          python ecc.py --history  # 이전 작업 이력 조회
        """)
    )
    parser.add_argument("goal", nargs="?", default=None)
    parser.add_argument("--host", default=os.environ.get("ECC_BOARD_HOST"))
    parser.add_argument("--user", default=os.environ.get("ECC_BOARD_USER"))
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--history", action="store_true", help="이전 goal 이력 출력")
    args = parser.parse_args()

    # --history 플래그
    if args.history:
        entries = load_history(last_n=30)
        print(f"\n{'─'*60}\n  📋 최근 goal 이력 ({len(entries)}개)\n{'─'*60}")
        print(format_history(entries))
        print()
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
        print("   export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    hint = ""
    if args.host: hint += f"\n\n[Connection hints: host={args.host}"
    if args.user: hint += f", user={args.user}"
    if args.port != 22: hint += f", port={args.port}"
    if hint: hint += "]"

    agent = AgentLoop(verbose=args.verbose)

    if args.goal:
        try:
            agent.run(args.goal + hint, max_turns=args.max_turns)
        except KeyboardInterrupt:
            print("\n\n  ⚡ 중단됨")
    else:
        # REPL
        model = os.environ.get("ECC_MODEL", "claude-sonnet-4-6")
        print(f"\n{'═'*60}\n  🤖 ECC  [{model}]\n  goal 입력. /quit 종료, /history 이력\n{'═'*60}\n")
        while True:
            try:
                prompt = f"ecc[{len(agent._session_messages)}]> " if agent._session_messages else "ecc> "
                raw = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  종료합니다.")
                break
            if not raw: continue
            if raw.lower() in ("/quit", "/q", "/exit"):
                print("  종료합니다."); break
            if raw.lower() in ("/new", "/reset"):
                agent._session_messages = []
                agent._session_goal = ""
                agent._session_todos = None
                agent._session_executor = None
                agent._session_memory = None
                print("  🆕 새 세션"); continue
            if raw.lower() in ("/history", "/h"):
                entries = load_history(last_n=20)
                print(format_history(entries)); continue
            try:
                agent.run(raw + hint, max_turns=args.max_turns)
            except KeyboardInterrupt:
                agent._save_partial_session()
                print("\n  ⚡ 중단. 다음 goal을 입력하세요.")
            except Exception as e:
                print(f"\n  ❌ 오류: {e}")
