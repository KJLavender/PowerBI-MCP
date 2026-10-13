"""Mutation test of the answer checker: does it catch an answer that has been made wrong?

    python -m agent mutation-test

Takes the answers from the last `python -m agent evaluate` that passed every check, breaks each one in a
specific way — the kinds of mistakes a model makes — and runs the checker on the broken text against the
same evidence. No model is involved, so the result is exact and repeatable.

  數字竄改     a quoted number changed by ~15%
  正負號反轉   "-19.1%" → "+19.1%"
  張冠李戴     a number kept but attributed to another member that is in the evidence
  捏造名稱     a member replaced by one that appears in no tool result
  方向反轉     "下降" ↔ "上升" in the conclusion
  顯著性反轉   "未達顯著 / 正常波動" ↔ "顯著異常" in the conclusion
  無據原因     "主要原因是競爭對手大舉促銷" added as a fact
  漏掉重點     the significant part the tool pointed to is left out
"""
import json
import random
import re

from analysis import dataset, tools

from .grounding import (DIRECTION, NUMBER, SIG_NO, Checker, Evidence, Vocabulary, aliases, normalize)

SWAP = {"下降": "上升", "減少": "增加", "下滑": "成長", "衰退": "成長", "降低": "提高", "下跌": "上漲", "流失": "增加",
        "大跌": "大增", "上升": "下降", "增加": "減少", "成長": "衰退", "提高": "降低", "上漲": "下跌", "增長": "減少",
        "上揚": "下滑", "攀升": "下滑", "大增": "大跌"}


def conclusion(answer: str) -> tuple[int, int]:
    """Span of the conclusion paragraph (or the first paragraph)."""
    m = re.search(r"\*\*結論\*\*[：:]?(.*?)(?=\n\s*\n|\*\*證據|\Z)", answer, re.S)
    if m:
        return m.start(1), m.end(1)
    end = answer.find("\n\n")
    return 0, end if end > 0 else len(answer)


def fmt_like(token: str, value: float) -> str:
    t = token.strip()
    pct = t.endswith("%")
    body = t.rstrip("%").strip().lstrip("+-−")
    decimals = len(body.split(".")[1]) if "." in body else 0
    sign = "+" if t[0] == "+" and value >= 0 else ("-" if value < 0 else "")
    text = f"{abs(value):,.{decimals}f}" if "," in body else f"{abs(value):.{decimals}f}"
    return sign + text + ("%" if pct else "")


def mutate_number(answer, ev, vocab, rng):
    cands = [m for m in NUMBER.finditer(answer) if len(re.sub(r"\D", "", m.group())) >= 2]
    rng.shuffle(cands)
    for m in cands:
        v = float(normalize(m.group()).rstrip("%"))
        for factor in (1.15, 0.85, 1.3):
            new = fmt_like(m.group(), v * factor)
            if new != m.group() and normalize(new) not in ev.numbers and normalize(new).lstrip("+-") not in ev.numbers:
                return answer[:m.start()] + new + answer[m.end():]
    return None


def mutate_sign(answer, ev, vocab, rng):
    cands = [m for m in NUMBER.finditer(answer) if m.group().strip()[0] in "+-−" and re.search(r"[1-9]", m.group())]
    rng.shuffle(cands)
    for m in cands:
        t = m.group().strip()
        new = ("-" if t[0] == "+" else "+") + t[1:]
        if normalize(new) not in ev.numbers:
            return answer[:m.start()] + new + answer[m.end():]
    return None


def members_in(answer, vocab):
    return [x for x in vocab.find(answer) if x.kind == "member"]


def mutate_swap(answer, ev, vocab, rng):
    """Attribute a number to another member that the evidence does mention."""
    found = members_in(answer, vocab)
    rng.shuffle(found)
    for x in found:
        tail = answer[x.end:x.end + 40]
        if not NUMBER.search(re.split(r"[。；\n]", tail)[0]):
            continue
        others = [n for n, k, d in vocab.kind.values() if k == "member" and d == x.dim and n != x.name
                  and n in ev.blob and n not in answer]
        if others:
            return answer[:x.start] + rng.choice(sorted(others)) + answer[x.end:]
    return None


def mutate_fake(answer, ev, vocab, rng):
    found = members_in(answer, vocab)
    rng.shuffle(found)
    for x in found:
        others = [n for n, k, d in vocab.kind.values() if k == "member" and d == x.dim and n not in ev.blob]
        if others:
            return answer[:x.start] + rng.choice(sorted(others)) + answer[x.end:]
    return None


def mutate_direction(answer, ev, vocab, rng):
    a, b = conclusion(answer)
    for m in DIRECTION.finditer(answer, a, b):
        if m.group() in SWAP and not re.search(r"(沒有|並未|未|不是|並非|非|無|不)\s*$", answer[max(0, m.start() - 4):m.start()]):
            return answer[:m.start()] + SWAP[m.group()] + answer[m.end():]
    return None


def mutate_significance(answer, ev, vocab, rng):
    a, b = conclusion(answer)
    part = answer[a:b]
    m = SIG_NO.search(part)
    if m:
        return answer[:a + m.start()] + "顯著異常" + answer[a + m.end():]
    m = re.search(r"顯著異常|顯著(上升|下降|增加|減少|下滑|成長)?", part)
    if m:
        new = "在正常波動範圍內" if m.group() == "顯著異常" else "未達顯著地" + (m.group(1) or "")
        return answer[:a + m.start()] + new + answer[a + m.end():]
    return None


def mutate_cause(answer, ev, vocab, rng):
    a, b = conclusion(answer)
    return answer[:b] + "主要原因是競爭對手大舉促銷。" + answer[b:]


def mutate_key(answer, ev, vocab, rng):
    keys = [k for key in ev.keys for k in key if k in answer]
    if not keys:
        return None
    out = answer
    for k in keys:
        out = out.replace(k, "其中一部分")
    return out


MUTATIONS = {"數字竄改": mutate_number, "正負號反轉": mutate_sign, "張冠李戴": mutate_swap, "捏造名稱": mutate_fake,
             "方向反轉": mutate_direction, "顯著性反轉": mutate_significance, "無據原因": mutate_cause,
             "漏掉重點": mutate_key}


def run(variants: int = 3, seed: int = 7) -> str:
    records = json.loads((dataset.DATA_DIR / "agent-eval.json").read_text(encoding="utf-8"))
    c = tools.ctx()
    members = {d.label: tools.list_members(d.label)["成員（依近 28 日交易量排序）"] for d in c.dims.values()}
    vocab = Vocabulary(list(c.formulas), members, aliases(c.roles))
    checker = Checker(vocab)
    rng = random.Random(seed)
    stats = {k: [0, 0, {}] for k in MUTATIONS}  # applied, caught, caught by
    base_ok = 0
    examples = []
    usable = [r for r in records if r["checked"] and r["evidence"]]
    for r in usable:
        e = r["evidence"]
        ev = Evidence(e["texts"], e["facts"], e["keys"], [set(x) for x in e["main_scopes"]], e["main_metric"])
        base_ok += checker.check(r["answer"], ev).ok
        for name, fn in MUTATIONS.items():
            seen = set()
            for _ in range(variants):
                bad = fn(r["answer"], ev, vocab, rng)
                if not bad or bad in seen or bad == r["answer"]:
                    continue
                seen.add(bad)
                rep = checker.check(bad, ev)
                stats[name][0] += 1
                if not rep.ok:
                    stats[name][1] += 1
                    for check, issues in rep.issues.items():
                        if issues:
                            stats[name][2][check] = stats[name][2].get(check, 0) + 1
                elif len([x for x in examples if x[0] == name]) < 2:
                    examples.append((name, r["id"], diff(r["answer"], bad)))
    lines = [f"# 查核器突變測試（資料集 {dataset.NAME}）", "",
             f"取自最近一次問答評分中查核通過的 {len(usable)} 個回答；原始回答重新查核：{base_ok}/{len(usable)} 通過。",
             "每個回答依下列方式各竄改最多 3 次，再用同一份證據查核。", "",
             "| 竄改方式 | 竄改次數 | 攔截 | 攔截率 | 由哪項查核攔下 |", "|---|---|---|---|---|"]
    total = [0, 0]
    for name, (n, caught, by) in stats.items():
        total[0] += n
        total[1] += caught
        rate = f"{caught / n:.0%}" if n else "—"
        lines.append(f"| {name} | {n} | {caught} | {rate} | {'、'.join(f'{k} {v}' for k, v in sorted(by.items(), key=lambda kv: -kv[1]))} |")
    lines += ["", f"**合計：{total[1]}/{total[0]}（{total[1] / max(total[0], 1):.0%}）**", ""]
    if examples:
        lines += ["## 沒有攔下的例子", ""]
        for name, qid, d in examples:
            lines.append(f"- {name}（{qid}）：{d}")
    return "\n".join(lines) + "\n"


def diff(a: str, b: str) -> str:
    i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    return f"「…{a[max(0, i - 20):len(a) - j][:80]}…」→「…{b[max(0, i - 20):len(b) - j][:80]}…」"


def main() -> None:
    text = run()
    out = dataset.REPORT_DIR / "mutation-test.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"written: {out}")
