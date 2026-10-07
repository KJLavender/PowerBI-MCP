"""Score the agent on agent/questions.yaml: right tools, right cause, every number grounded."""
import re
from pathlib import Path

import yaml

from .host import ROOT, Agent

QUESTIONS = Path(__file__).with_name("questions.yaml")


async def run(args) -> None:
    items = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    if args.only:
        items = [q for q in items if q["id"] == args.only]
    rows, transcript = [], [f"# Agent 問答評分（模型 {args.model}）", ""]
    async with Agent(args.model, verbose=args.verbose, think=args.think) as agent:
        for q in items:
            turn = await agent.ask(q["question"])
            used = {c["tool"] for c in turn.tool_calls}
            tool_ok = bool(used & set(q["tools"]))
            missing = [g for g in q["expect"] if not re.search(g, turn.answer)]
            missing += [f"不可出現：{g}" for g in q.get("forbid", []) if re.search(g, turn.answer)]
            grounded = not turn.ungrounded
            ok = tool_ok and not missing and grounded
            rows.append((q["id"], ok, tool_ok, not missing, grounded, turn.retries, turn.seconds))
            print(f"{'PASS' if ok else 'FAIL'} {q['id']:<15} tools={sorted(used)} keywords={'ok' if not missing else missing} "
                  f"grounded={grounded} rewrites={turn.retries} {turn.seconds:.0f}s", flush=True)
            transcript += [f"## {q['id']}：{q['question']}", "",
                           f"- 工具：{', '.join(c['tool'] + str(c['args']) for c in turn.tool_calls)}",
                           f"- 結果：{'通過' if ok else '未通過'}（工具 {tool_ok}、關鍵字 {not missing}、數字查核 {grounded}、重寫 {turn.retries} 次）",
                           "", turn.answer, ""]
    passed = sum(r[1] for r in rows)
    summary = (f"\n通過 {passed}/{len(rows)}｜工具正確 {sum(r[2] for r in rows)}｜原因正確 {sum(r[3] for r in rows)}｜"
               f"數字全數有據 {sum(r[4] for r in rows)}｜平均 {sum(r[6] for r in rows) / len(rows):.0f} 秒")
    print(summary)
    transcript.insert(2, summary.strip() + "\n")
    out = ROOT / "reports" / f"agent-evaluation-{args.model.replace(':', '_').replace('/', '_')}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(transcript), encoding="utf-8")
    print(f"transcript: {out}")
