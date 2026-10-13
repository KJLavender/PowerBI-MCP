"""Local agent: Ollama model + the read-only MCP server, with a mechanical check of every answer.

Nothing leaves the machine: the MCP server runs as a child process over stdio and the model is
served by the local Ollama instance.

The model never sees the tools' structured claims (_facts / _key); the checker compares the answer
against them (see grounding.py) and asks for a rewrite, or replaces the answer, when they disagree.
"""
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

import ollama
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from analysis import dataset
from analysis.context import DEFAULT_ROLES

from . import grounding

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "qwen3.5:4b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
CONFIG = yaml.safe_load(dataset.CONFIG.read_text(encoding="utf-8")) or {}
HINTS = CONFIG.get("agent", {})
DATE = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")

SYSTEM = """\
你是公司內部的 Power BI 報表分析助理，只能透過工具查詢資料。資料最新日期是 {latest}。

可用指標：{metrics}
維度與成員（filters 用「維度名稱: 成員」）：
{dims}
{examples}

規則：
1. 回答中的每一個數字、百分比、日期都必須「原文照抄」工具結果中的文字（含正負號、%、「個百分點」）。
   不可以自己計算、加總、換算、估計或四捨五入。工具沒有給的數字就不要寫。
2. 問「為什麼」時，一定先呼叫 explain_change（metric、filters、date、window）。
   - date 一律寫成 YYYY-MM-DD；問「整個月」寫成 YYYY-MM，工具會用該月的近 28 日。
   - 若工具回傳「錯誤」，依錯誤訊息修正參數後再呼叫；不可以在沒有拿到資料的情況下作答。
{window_rules}
   - filters 是物件，例如 {{"維度名稱": "成員"}}，不要寫成字串。
   - 引用拆解結果時，「佔總變動 77%」是佔比，不是變化率，不可寫成「-77%」。
   - 結論以工具結果的「重點」為準：方向（上升或下降）、是否顯著、顯著異常的是哪個部分，都要跟「重點」與「判讀」一致。
     問題的說法若與資料不符（例如問「為什麼下降」但其實上升），要直接指出實際的方向。
   - 名稱不確定時先呼叫 list_members 或 describe_model，不要猜。
3. 原因只能說到資料能證明的程度，例如「下滑集中在某個品類，佔總變動 84%」。
   資料無法說明的業務原因（競爭對手、天氣、行銷活動內容）只能寫成「可能」，並說明「資料無法判斷，需業務確認」。
4. 若結果的「脈絡」提到資料品質問題，要優先指出「可能是資料缺漏，而非實際營運變化」。
5. 若變化不顯著，要說明變化在正常波動範圍內；若判讀是「樣本太少」，要說資料量不足以判斷，不可以下結論。
6. 用繁體中文，格式：
   **結論**：一到兩句。
   **證據**：條列，每點引用工具回傳的文字。
   **限制**：資料無法回答的部分。
"""

DEFAULT_WINDOW_RULES = """\
- 問「某一天」（例如 9/17、昨天）用 window="day"；「昨天 / 最近一天」= {latest}。
- 「這週 / 最近一週」用 window="week"；「這個月 / 最近幾週」用 window="month"；「跟去年比 / 長期流失」用 window="year"。"""


@dataclass
class Turn:
    question: str
    answer: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # tool, args, result (as the model saw it), facts, key
    ungrounded: list[str] = field(default_factory=list)   # issues left after the rewrites (empty = passed)
    retries: int = 0
    seconds: float = 0.0
    evidence: list[str] = field(default_factory=list)     # tool results this answer may quote
    check: dict = field(default_factory=dict)             # per-check counts and issues of the final answer
    verified_with: dict = field(default_factory=dict)     # the evidence the final answer was checked against
    fallback: bool = False                                # the model's text failed; the tool's reading is shown

    @property
    def facts(self) -> list[dict]:
        return [f for c in self.tool_calls for f in c.get("facts", [])]

    @property
    def main_scopes(self) -> list[set]:
        """Scopes the analysis calls were about (a follow-up may leave them implied)."""
        out = []
        for c in self.tool_calls:
            if c["tool"] in ("explain_change", "metric_change") and c.get("facts"):
                out.append(set(c["facts"][0]["scope"].values()))
        return out


class Agent:
    def __init__(self, model: str = DEFAULT_MODEL, verbose: bool = False, max_steps: int = 6,
                 num_ctx: int = 16384, think: bool = False):
        self.model, self.verbose, self.max_steps = model, verbose, max_steps
        self.options = {"num_ctx": num_ctx, "temperature": 0.1}
        self.think = think
        self.llm = ollama.AsyncClient(host=OLLAMA_HOST)
        self.stack = AsyncExitStack()
        self.session: ClientSession | None = None
        self.tools: list[dict] = []
        self.latest = ""
        self.context_members: set[str] = set()

    async def __aenter__(self):
        params = StdioServerParameters(command=sys.executable, args=["-m", "analysis.mcp_server"], cwd=str(ROOT),
                                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        read, write = await self.stack.enter_async_context(stdio_client(params))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                        "parameters": t.input_schema}} for t in listed.tools]
        model_info = json.loads(await self.call("describe_model", {}))
        self.latest = model_info["資料期間"].split("～")[-1].strip()
        self.metrics = "、".join(model_info["指標"])
        lines, members = [], {}
        for label in model_info["維度"]:
            ms = json.loads(await self.call("list_members", {"dimension": label}))["成員（依近 28 日交易量排序）"]
            members[label] = ms
            shown = "、".join(ms) if len(ms) <= 10 else f"{len(ms)} 個，例如 {'、'.join(ms[:5])}…（不確定時用 list_members 查）"
            lines.append(f"- {label}：{shown}")
        self.dims = "\n".join(lines)
        roles = {**DEFAULT_ROLES, **CONFIG.get("roles", {})}
        self.checker = grounding.Checker(grounding.Vocabulary(list(model_info["指標"]), members, grounding.aliases(roles)))
        return self

    async def __aexit__(self, *exc):
        await self.stack.aclose()

    async def call(self, name: str, args: dict) -> str:
        res = await self.session.call_tool(name, args)
        return "\n".join(c.text for c in res.content if getattr(c, "text", None))

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)

    def system(self) -> str:
        rules = HINTS.get("window_rules") or DEFAULT_WINDOW_RULES
        return SYSTEM.format(latest=self.latest, metrics=self.metrics, dims=self.dims,
                             examples=HINTS.get("examples", ""),
                             window_rules="\n".join("   " + line for line in rules.format(latest=self.latest).splitlines()))

    async def chat(self, messages: list[dict], tools: bool = True):
        return await self.llm.chat(model=self.model, messages=messages, tools=self.tools if tools else None,
                                   options=self.options, think=self.think)

    async def _loop(self, messages: list[dict], turn: Turn, evidence_text: list[str], emit) -> str:
        """Let the model call tools until it answers."""
        for _ in range(self.max_steps):
            msg = (await self.chat(messages)).message
            if not msg.tool_calls:
                return msg.content or ""
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                name, args = tc.function.name, dict(tc.function.arguments or {})
                self.log(f"  → {name}({json.dumps(args, ensure_ascii=False)})")
                await emit({"type": "tool", "name": name, "args": args})
                note = fix_date(turn.question, name, args)
                visible, facts, key = grounding.split_result(await self.call(name, args))
                if note:
                    visible = visible[:-1] + f', "參數校正": "{note}"}}' if visible.endswith("}") else visible
                turn.tool_calls.append({"tool": name, "args": args, "result": visible, "facts": facts, "key": key})
                evidence_text.append(visible)
                messages.append({"role": "tool", "content": visible, "tool_name": name})
        await emit({"type": "status", "text": "整理回答中"})
        msg = (await self.chat(messages + [{"role": "user", "content": "請根據以上工具結果直接作答。"}], tools=False)).message
        return msg.content or ""

    def verify(self, answer: str, turn: Turn, history: list[Turn], evidence_text: list[str]) -> grounding.Report:
        past = history[-3:]
        ev = grounding.Evidence(
            texts=evidence_text,
            facts=turn.facts + [f for t in reversed(past) for f in t.facts],
            keys=[c.get("key") or [] for c in turn.tool_calls if c["tool"] == "explain_change"][-1:],
            main_scopes=turn.main_scopes + [s for t in past for s in t.main_scopes],
            main_metric=next((c["facts"][0]["metric"] for c in reversed(turn.tool_calls)
                              if c["tool"] in ("explain_change", "metric_change") and c.get("facts")
                              and c["args"].get("metric")), None))
        turn.verified_with = {"texts": ev.texts, "facts": ev.facts, "keys": ev.keys,
                              "main_scopes": [sorted(x) for x in ev.main_scopes], "main_metric": ev.main_metric}
        report = self.checker.check(strip_think(answer), ev)
        claim = grounding.unsupported_claim(answer, evidence_text)
        if claim:
            report.add("顯著性", claim)
        self.check_dates(turn, report)
        self.check_scope(turn, history, report)
        return report

    def check_scope(self, turn: Turn, history: list[Turn], report: grounding.Report) -> None:
        """The analysis must be about what the question names: no member dropped, none added on the model's own."""
        asked = {m.name for m in self.checker.vocab.find(turn.question) if m.kind == "member"}
        calls = [c for c in turn.tool_calls if c["tool"] in ("explain_change", "metric_change") and c.get("facts")]
        if not asked or not calls:
            return
        allowed = asked | {m for t in history[-3:] for s in t.main_scopes for m in s} | self.context_members
        scopes = [set(c["facts"][0]["scope"].values()) for c in calls]
        ok = any(asked <= s <= allowed for s in scopes)
        report.add("範圍", None if ok else
                   f"問題問的是「{' × '.join(sorted(asked))}」，但工具查的範圍是「{' × '.join(sorted(scopes[-1])) or '整體'}」；"
                   "filters 只能放問題提到的成員，請重新查詢")

    @staticmethod
    def check_dates(turn: Turn, report: grounding.Report) -> None:
        """A question about a given day must be analysed for that day (not silently for its month)."""
        asked = {f"{y}-{int(m):02d}-{int(d):02d}" for y, m, d in DATE.findall(turn.question)}
        calls = [c for c in turn.tool_calls if c["tool"] in ("explain_change", "metric_change", "daily_report")
                 and not c["result"].startswith('{"錯誤"')]
        if not asked or not calls:
            return
        norm = lambda v: next((f"{y}-{int(m):02d}-{int(d):02d}" for y, m, d in DATE.findall(v)), v)  # noqa: E731
        used = {norm(str(c["args"].get("date") or "")) for c in calls}
        report.add("日期", None if asked & used else
                   f"問題問的是 {'、'.join(sorted(asked))}，但工具查的是 {'、'.join(sorted(u or '最新一天' for u in used))}；"
                   f"請用 date=\"{min(asked)}\" 重新查詢")

    async def ask(self, question: str, history: list[Turn] | None = None, on_event=None,
                  context: str | None = None) -> Turn:
        """Answer one question. history: earlier turns of the same conversation (for follow-ups such as
        「那南區呢？」); their tool results stay valid evidence. on_event: async callback for progress.
        context: what the user currently has selected next to the report (filters, date, window)."""
        async def emit(event: dict) -> None:
            if on_event:
                await on_event(event)

        history = history or []
        t0 = time.time()
        turn = Turn(question)
        messages = [{"role": "system", "content": self.system()}]
        evidence_text = [question, self.latest]
        for past in history[-3:]:
            messages += [{"role": "user", "content": past.question},
                         {"role": "assistant", "content": past.answer.split("\n\n> ⚠️")[0]}]
            evidence_text += past.evidence + [past.question]
        self.context_members = {m.name for m in self.checker.vocab.find(context or "") if m.kind == "member"}
        if context:
            messages.append({"role": "system", "content":
                             f"使用者目前在報表上的篩選：{context}。問題若沒有指定日期、範圍或時間窗，就沿用這些篩選"
                             "（filters 用同樣的維度與成員）；問題有指定時，以問題為準。"})
            evidence_text.append(context)
        messages.append({"role": "user", "content": question})
        answer = await self._loop(messages, turn, evidence_text, emit)

        # every answer is checked against the tool results; failures are sent back for a rewrite
        report = self.verify(answer, turn, history, evidence_text)
        while not report.ok and turn.retries < 3:
            turn.retries += 1
            fb = grounding.feedback(report)
            self.log(f"  ── 草稿 ──\n{answer}\n  ✗ {fb}")
            await emit({"type": "retry", "text": "自動查核未通過，要求模型依工具結果重寫",
                        "issues": report.all_issues()[:5]})
            messages += [{"role": "assistant", "content": answer}, {"role": "user", "content": fb}]
            answer = await self._loop(messages, turn, evidence_text, emit)
            report = self.verify(answer, turn, history, evidence_text)
        turn.evidence = [e for e in evidence_text if e.startswith("{")]
        turn.check = report.summary()
        turn.ungrounded = report.all_issues()
        if not report.ok:
            # still failing after rewrites: don't show the model's text. If the analysis itself answered the
            # question, show its own (deterministic) reading instead; otherwise say what went wrong.
            asked = {m.name for m in self.checker.vocab.find(turn.question) if m.kind == "member"}
            turn.answer = tool_answer(turn, asked | self.context_members) or failure_text(turn, report)
            turn.fallback = True
        else:
            turn.answer = strip_think(answer)
        turn.seconds = time.time() - t0
        return turn


def tool_answer(turn: Turn, asked: set[str]) -> str | None:
    """The answer the analysis tool computed, without the model: 重點, 判讀 and the main numbers.
    Only from a call about what was asked (same members, same day): never from a call the checks rejected."""
    days = {f"{y}-{int(m):02d}-{int(d):02d}" for y, m, d in DATE.findall(turn.question)}

    def matches(c: dict) -> bool:
        scope = set(c["facts"][0]["scope"].values())
        return (not asked or asked <= scope <= asked) and (not days or str(c["args"].get("date")) in days)

    call = next((c for c in reversed(turn.tool_calls) if c["tool"] == "explain_change" and c.get("facts")
                 and not c["result"].startswith('{"錯誤"') and matches(c)), None)
    if not call:
        return None
    r = json.loads(call["result"])
    lines = [f"**結論（分析工具的判讀）**：{r['重點']}。", "", "**證據**：",
             f"- {r['問題']}：{r['變化']['display']}，判讀「{r['判讀']}」"]
    lines += [f"- 深入一層：{x}" for x in r.get("深入一層", [])[:3]]
    lines += [f"- 量價判讀：{x}" for x in r.get("量價判讀", [])]
    if r.get("日期解讀"):
        lines.append(f"- 日期解讀：{r['日期解讀']}")
    lines += ["", "**限制**：語言模型的說明未通過自動查核，以上直接列出分析工具的計算與判讀；"
                  "資料無法說明業務原因（如競爭對手、行銷活動），需業務確認。"]
    return "\n".join(lines)


def failure_text(turn: Turn, report: grounding.Report) -> str:
    errors = [json.loads(c["result"]).get("錯誤") for c in turn.tool_calls if c.get("result", "").startswith('{"錯誤"')]
    hint = HINTS.get("ask_hint") or "**日期**、**指標**與**範圍**"
    return ("**這次無法產生可靠的回答。** 模型的回答與工具結果不一致，已依查核規則拿掉。\n\n"
            + (f"- 工具回報：{errors[-1]}\n" if errors else "")
            + "".join(f"- {i}\n" for i in report.all_issues()[:4])
            + f"\n建議換個方式問，寫清楚{hint}。")


def fix_date(question: str, tool: str, args: dict) -> str | None:
    """The question names a day but the model asked for its month ("2015-10-11" → "2015-10"): use the day."""
    if tool not in ("explain_change", "metric_change", "daily_report") or not isinstance(args.get("date"), str):
        return None
    days = sorted({f"{y}-{int(m):02d}-{int(d):02d}" for y, m, d in DATE.findall(question)})
    month = re.fullmatch(r"\s*(\d{4})\s*[-/年.]\s*(\d{1,2})\s*月?\s*", args["date"])
    if len(days) == 1 and month and days[0].startswith(f"{month[1]}-{int(month[2]):02d}"):
        old, args["date"] = args["date"], days[0]
        return f"問題問的是 {days[0]}，date 已由 {old} 改為 {days[0]}"
    return None


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
