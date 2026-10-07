"""Ask questions about the Power BI report with a local model.

  python -m agent ask "為什麼 9/17 北區的銷售額下降？" [-v] [--model qwen3.5:4b]
  python -m agent chat [-v]
  python -m agent evaluate [--model ...]       questions in agent/questions.yaml
"""
import argparse
import asyncio
import sys

from .host import DEFAULT_MODEL, Agent


def show(turn) -> None:
    print(turn.answer)
    tools = ", ".join(c["tool"] for c in turn.tool_calls) or "（無）"
    print(f"\n— 工具：{tools}｜重寫 {turn.retries} 次｜{turn.seconds:.0f} 秒", file=sys.stderr)


async def ask(args) -> None:
    async with Agent(args.model, verbose=args.verbose, think=args.think) as agent:
        show(await agent.ask(args.question))


async def chat(args) -> None:
    async with Agent(args.model, verbose=args.verbose, think=args.think) as agent:
        print(f"模型 {args.model}，最新營業日 {agent.latest}。輸入問題，空白行離開。")
        while True:
            q = input("\n> ").strip()
            if not q:
                break
            show(await agent.ask(q))


def main() -> None:
    p = argparse.ArgumentParser(prog="agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("-v", "--verbose", action="store_true", help="顯示工具呼叫過程")
    p.add_argument("--think", action="store_true", help="啟用模型的思考模式（較慢）")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ask")
    s.add_argument("question")
    s.set_defaults(func=ask)
    sub.add_parser("chat").set_defaults(func=chat)
    s = sub.add_parser("evaluate")
    s.add_argument("--only", help="只跑某個題目 id")
    s.set_defaults(func=lambda a: __import__("agent.evaluate", fromlist=["run"]).run(a))
    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
