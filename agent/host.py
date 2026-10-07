"""Local agent: Ollama model + the read-only MCP server, with a grounding check on every answer.

Nothing leaves the machine: the MCP server runs as a child process over stdio and the model is
served by the local Ollama instance.
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
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import grounding

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "qwen3.5:4b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

SYSTEM = """\
你是公司內部的 Power BI 報表分析助理，只能透過工具查詢資料。最新營業日是 {latest}。

可用指標：{metrics}
維度與成員（filters 用「維度名稱: 成員」）：
{dims}
例如「VIP 顧客的訂單數」→ metric="訂單數"、filters={{"顧客分群": "VIP"}}；「北區 3C配件」→ filters={{"區域": "北區", "品類": "3C配件"}}。

規則：
1. 回答中的每一個數字、百分比、日期都必須「原文照抄」工具結果中的文字（含正負號、%、「個百分點」）。
   不可以自己計算、加總、換算、估計或四捨五入。工具沒有給的數字就不要寫。
2. 問「為什麼」時，一定先呼叫 explain_change（metric、filters、date、window）。
   - 問「某一天」（例如 9/17、昨天）用 window="day"；「昨天 / 最近一天」= {latest}。
   - 「這週 / 最近一週」用 window="week"；「這個月 / 最近幾週」用 window="month"；「跟去年比 / 長期流失」用 window="year"。
   - filters 是物件，例如 {{"區域": "北區"}}，不要寫成字串。
   - 引用拆解結果時，「佔總變動 77%」是佔比，不是變化率，不可寫成「-77%」。
   - 結論要依據「判讀」與「深入一層」：若本範圍未達顯著但「深入一層」有顯著異常的子範圍，結論應指出那個子範圍。
   - 名稱不確定時先呼叫 list_members 或 describe_model，不要猜。
3. 原因只能說到資料能證明的程度，例如「下滑集中在 3C配件，佔總變動 84%」。
   資料無法說明的業務原因（競爭對手、天氣、行銷活動內容）要明確說「資料無法判斷，需業務確認」。
4. 若結果的「脈絡」提到資料品質問題，要優先指出「可能是資料缺漏，而非實際營運變化」。
5. 若變化不顯著（顯著: false），要說明變化在正常波動範圍內。
6. 用繁體中文，格式：
   **結論**：一到兩句。
   **證據**：條列，每點引用工具回傳的文字。
   **限制**：資料無法回答的部分。
"""


@dataclass
class Turn:
    question: str
    answer: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)
    retries: int = 0
    seconds: float = 0.0
    evidence: list[str] = field(default_factory=list)  # tool results this answer may quote


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
        lines = []
        for label in model_info["維度"]:
            members = json.loads(await self.call("list_members", {"dimension": label}))["成員（依近 28 日交易量排序）"]
            shown = "、".join(members) if len(members) <= 10 else f"{len(members)} 個，例如 {'、'.join(members[:5])}…（不確定時用 list_members 查）"
            lines.append(f"- {label}：{shown}")
        self.dims = "\n".join(lines)
        return self

    async def __aexit__(self, *exc):
        await self.stack.aclose()

    async def call(self, name: str, args: dict) -> str:
        res = await self.session.call_tool(name, args)
        return "\n".join(c.text for c in res.content if getattr(c, "text", None))

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)

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
                result = await self.call(name, args)
                turn.tool_calls.append({"tool": name, "args": args, "result": result})
                evidence_text.append(result)
                messages.append({"role": "tool", "content": result, "tool_name": name})
        await emit({"type": "status", "text": "整理回答中"})
        msg = (await self.chat(messages + [{"role": "user", "content": "請根據以上工具結果直接作答。"}], tools=False)).message
        return msg.content or ""

    def problems(self, answer: str, turn: Turn, evidence_text: list[str]) -> tuple[list[str], str | None]:
        """(ungrounded numbers, feedback for the model or None)."""
        bad = grounding.ungrounded(answer, grounding.evidence_set(*evidence_text))
        if bad:
            return bad, (f"以下數字在工具結果中找不到：{'、'.join(bad)}。"
                         "請只使用工具結果中原文出現的數字重寫答案，找不到的就刪掉。")
        claim = grounding.unsupported_claim(answer, evidence_text)
        if claim:
            return [], claim
        return [], None

    async def ask(self, question: str, history: list[Turn] | None = None, on_event=None) -> Turn:
        """Answer one question. history: earlier turns of the same conversation (for follow-ups such as
        「那南區呢？」); their tool results stay valid evidence. on_event: async callback for progress."""
        async def emit(event: dict) -> None:
            if on_event:
                await on_event(event)

        t0 = time.time()
        turn = Turn(question)
        messages = [{"role": "system", "content": SYSTEM.format(latest=self.latest, metrics=self.metrics, dims=self.dims)}]
        evidence_text = [question, self.latest]
        for past in (history or [])[-3:]:
            messages += [{"role": "user", "content": past.question},
                         {"role": "assistant", "content": past.answer.split("\n\n> ⚠️")[0]}]
            evidence_text += past.evidence + [past.question]
        messages.append({"role": "user", "content": question})
        answer = await self._loop(messages, turn, evidence_text, emit)

        # grounding: numbers must come from tool results; significance claims need a significance tool
        bad, feedback = self.problems(answer, turn, evidence_text)
        while feedback and turn.retries < 2:
            turn.retries += 1
            self.log(f"  ✗ {feedback}")
            await emit({"type": "retry", "text": "自動查核未通過，要求模型依工具結果重寫"})
            messages += [{"role": "assistant", "content": answer}, {"role": "user", "content": feedback}]
            answer = await self._loop(messages, turn, evidence_text, emit)
            bad, feedback = self.problems(answer, turn, evidence_text)
        turn.evidence = [e for e in evidence_text if e.startswith("{")]
        turn.answer = strip_think(answer)
        turn.ungrounded = bad + ([feedback] if feedback and not bad else [])
        if bad:
            turn.answer += f"\n\n> ⚠️ 自動查核：以下數字無法在工具結果中找到，請勿採信：{'、'.join(bad)}"
        elif feedback:
            turn.answer += "\n\n> ⚠️ 自動查核：回答中的「是否異常」判斷沒有工具依據，請勿採信。"
        turn.seconds = time.time() - t0
        return turn


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
