"""Score the agent on the dataset's question set: right tools, right cause, answer passes every check.

    python -m agent evaluate [--only ID]

Questions: agent/questions.yaml (retail) or datasets/<name>/questions.yaml. Their expected keywords come from
the injected scenarios, which the agent cannot see. The answers and the evidence they were checked against
are kept in data/.../agent-eval.json for the mutation test (python -m agent mutation-test).
"""
import json
import re

import yaml

from analysis import dataset

from .grounding import CHECKS
from .host import Agent


def check_line(check: dict) -> str:
    return "｜".join(f"{k} {v['checked']}" + (" ✓" if not v["issues"] else f" ✗{len(v['issues'])}")
                    for k, v in check.items() if v["checked"] or v["issues"])


async def run(args) -> None:
    items = yaml.safe_load(dataset.QUESTIONS.read_text(encoding="utf-8"))
    if args.only:
        items = [q for q in items if q["id"] == args.only]
    rows, records = [], []
    transcript = [f"# Agent 問答評分（資料集 {dataset.NAME}，模型 {args.model}）", ""]
    async with Agent(args.model, verbose=args.verbose, think=args.think) as agent:
        for q in items:
            turn = await agent.ask(q["question"])
            used = {c["tool"] for c in turn.tool_calls}
            tool_ok = bool(used & set(q["tools"]))
            missing = [g for g in q["expect"] if not re.search(g, turn.answer)]
            missing += [f"不可出現：{g}" for g in q.get("forbid", []) if re.search(g, turn.answer)]
            checked = not turn.ungrounded
            ok = tool_ok and not missing and checked
            # what the user ends up reading: the model's checked answer, or the tool's own reading when it failed
            shown = "模型回答" if checked else ("工具判讀" if turn.fallback and "分析工具的判讀" in turn.answer else "無法回答")
            user_ok = tool_ok and not missing and shown != "無法回答"
            rows.append((q["id"], ok, tool_ok, not missing, checked, turn.retries, turn.seconds, user_ok, shown))
            print(f"{'PASS' if ok else 'FAIL'} {q['id']:<20} tools={sorted(used)} keywords={'ok' if not missing else missing} "
                  f"checks={'ok' if checked else turn.ungrounded[:2]} rewrites={turn.retries} shown={shown} "
                  f"{turn.seconds:.0f}s", flush=True)
            transcript += [f"## {q['id']}：{q['question']}", "",
                           f"- 工具：{', '.join(c['tool'] + json.dumps(c['args'], ensure_ascii=False) for c in turn.tool_calls)}",
                           f"- 結果：{'通過' if ok else '未通過'}（工具 {tool_ok}、關鍵字 {not missing}、查核 {checked}、"
                           f"重寫 {turn.retries} 次）；使用者看到：{shown}{'（正確）' if user_ok else ''}",
                           f"- 查核：{check_line(turn.check)}"]
            if missing:
                transcript.append(f"- 缺少：{missing}")
            transcript += ["", turn.answer, ""]
            records.append({"id": q["id"], "question": q["question"], "answer": turn.answer, "passed": ok,
                            "checked": checked, "evidence": turn.verified_with})
    passed = sum(r[1] for r in rows)
    summary = (f"\n通過 {passed}/{len(rows)}｜工具正確 {sum(r[2] for r in rows)}｜原因正確 {sum(r[3] for r in rows)}｜"
               f"查核全數通過 {sum(r[4] for r in rows)}｜重寫 {sum(r[5] for r in rows)} 次｜"
               f"平均 {sum(r[6] for r in rows) / len(rows):.0f} 秒\n"
               f"使用者拿到正確答案 {sum(r[7] for r in rows)}/{len(rows)}（模型回答 {sum(r[8] == '模型回答' and r[7] for r in rows)}、"
               f"改顯示工具判讀 {sum(r[8] == '工具判讀' and r[7] for r in rows)}）｜"
               f"查核通過卻答錯 {sum(r[4] and not (r[2] and r[3]) for r in rows)}")
    print(summary)
    transcript.insert(2, summary.strip() + f"\n\n查核項目：{'、'.join(CHECKS)}（見 docs/agent.md）\n")
    name = args.model.replace(":", "_").replace("/", "_")
    out = dataset.REPORT_DIR / f"agent-evaluation-{name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(transcript), encoding="utf-8")
    if not args.only:
        dataset.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (dataset.DATA_DIR / "agent-eval.json").write_text(json.dumps(records, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
    print(f"transcript: {out}")
