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
        Examples:
          python ecc.py "check system status"
          python ecc.py "drive at 1m/s for 3 seconds"
          python ecc.py --host 192.168.1.100 "drive vehicle"
          python ecc.py   # REPL mode
          python ecc.py --history  # Show goal history
        """)
    )
    parser.add_argument("goal", nargs="?", default=None)
    parser.add_argument("--host", default=os.environ.get("ECC_BOARD_HOST"))
    parser.add_argument("--user", default=os.environ.get("ECC_BOARD_USER"))
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--max-turns", type=int, default=100)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--history", action="store_true", help="Show previous goal history")
    args = parser.parse_args()

    # --history flag
    if args.history:
        entries = load_history(last_n=30)
        print(f"\n{'─'*60}\n  📋 Recent goal history ({len(entries)})\n{'─'*60}")
        print(format_history(entries))
        print()
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY environment variable is not set.")
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
            print("\n\n  ⚡ Interrupted")
    else:
        # REPL
        model = os.environ.get("ECC_MODEL", "claude-sonnet-4-6")
        print(f"\n{'═'*60}\n  🤖 ECC  [{model}]\n  Enter goal. /quit to exit, /history for history\n{'═'*60}\n")
        while True:
            try:
                prompt = f"ecc[{len(agent._session_messages)}]> " if agent._session_messages else "ecc> "
                raw = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Exiting..")
                break
            if not raw: continue
            if raw.lower() in ("/quit", "/q", "/exit"):
                print("  Exiting.."); break
            if raw.lower() in ("/new", "/reset"):
                agent._session_messages = []
                agent._session_goal = ""
                agent._session_todos = None
                agent._session_executor = None
                agent._session_memory = None
                print("  🆕 New session"); continue
            if raw.lower() in ("/history", "/h"):
                entries = load_history(last_n=20)
                print(format_history(entries)); continue
            try:
                agent.run(raw + hint, max_turns=args.max_turns)
            except KeyboardInterrupt:
                agent._save_partial_session()
                print("\n  ⚡ Interrupted. Enter next goal..")
            except Exception as e:
                print(f"\n  ❌ Error: {e}")
