"""Mechanical checks of an answer against the tool results it was built from.

The model only phrases; everything it states must be traceable to a tool result:

  日期    the analysis was run for the date the question asked about
  範圍    …and for the members the question names (none dropped, none added by the model)
  數字    every number appears in a tool result or the question (sign-aware; a % matches a %)
  名稱    every metric and dimension member it names appears in a tool result or the question
  對應    a number is attached to the right thing: the members / metric written just before it appear
          together with that number in one tool-result entry ("電商平台 -75,386" fails when -75,386 is 3C配件's)
  方向    "X 上升 / 下降" agrees with the sign of X's change in the tool results
  顯著性  "X 顯著異常" / "X 在正常波動範圍內" agrees with the tool's verdict for X
  原因    business causes the data cannot show (competitors, weather …) are stated only as possibilities
  重點    the part the tool found significant (and that explains the change) is mentioned

No model is involved in checking: an answer either matches the structured results or it is rejected.
"""
import json
import re
from dataclasses import dataclass, field

# digits not glued to latin letters, so names like "3C配件", "30W", "600ml" are skipped
NUMBER = re.compile(r"(?<![A-Za-z\d.])[-+−]?\d[\d,]*(?:\.\d+)?(?![\dA-Za-z])(?:\s*%)?")


def normalize(token: str) -> str:
    """Keep an explicit sign ('+'/'-'), drop separators and spaces."""
    return token.replace(",", "").replace(" ", "").replace("−", "-")


def numbers(text: str) -> list[str]:
    return [normalize(m.group()) for m in NUMBER.finditer(text)]


def evidence_set(*texts: str) -> set[str]:
    out = set()
    for t in texts:
        for n in numbers(t):
            unsigned = n.lstrip("+-")
            out.update({n, unsigned, n.rstrip("%"), unsigned.rstrip("%")})
            if n.startswith("-"):
                out.add("-" + unsigned)
    return out


def grounded(n: str, evidence: set[str]) -> bool:
    signed, bare, pct = n[0] in "+-", n.lstrip("+-"), n.endswith("%")
    if signed:
        return n in evidence  # sign must match: "佔總變動 77%" does not ground "-77%"
    if pct:
        return bare in evidence
    return bare in evidence or bare.rstrip("%") in evidence


def ungrounded(answer: str, evidence: set[str]) -> list[str]:
    """Numbers in the answer that do not appear in the evidence."""
    bad = []
    for n in numbers(answer):
        if grounded(n, evidence):
            continue
        if n[0] not in "+-" and not n.endswith("%") and "." not in n and len(n) == 1:
            continue  # list markers / ordinals
        bad.append(n)
    return list(dict.fromkeys(bad))


# words that make another thing the subject of a following direction word ("毛利率商品占比提高")
SUBJECT = re.compile(r"占比|佔比|比重|比例|商品|品項|數量|件數|筆數|金額|成本|幅度|差距|門檻")

CLAIM = re.compile(r"顯著|正常波動|波動範圍|異常|流失")


def unsupported_claim(answer: str, evidence_texts: list[str]) -> str | None:
    """A verdict ("顯著" / "正常波動" / "流失" …) needs a tool result that actually judged significance."""
    if not CLAIM.search(answer):
        return None
    if any('"判讀"' in t or '"顯著"' in t or '"顯著指標"' in t for t in evidence_texts):
        return None
    return ("你的回答判斷了是否顯著／正常／流失，但目前的工具結果沒有做這個判斷。"
            "請先呼叫 metric_change 或 explain_change（與去年同期比較用 window=\"year\"），再依其「判讀」作答。")


# ------------------------------------------------------------------------------------ text structure
UP = r"上升|增加|成長|增長|提高|上揚|走高|攀升|回升|變多|擴大|大增|暴增|上漲|升高|增多"
DOWN = r"下降|減少|下滑|衰退|降低|下跌|走低|流失|萎縮|變少|大跌|縮減|下挫|跌|掉"
DIRECTION = re.compile(rf"(?P<up>{UP})|(?P<down>{DOWN})")
NEGATION = re.compile(r"(沒有|並未|未|不是|並非|而非|非|無|不)\s*$")
REPORTED = re.compile(r"問題(中|裡)?(說|提到|問)|[您你](說|提到|問)|提問|以為|所說的")
SIG_NO = re.compile(r"未達[^，。；]{0,6}顯著|不[^，。；]{0,3}顯著|未[^，。；]{0,3}顯著|沒有[^，。；]{0,4}(顯著|異常)|非顯著|"
                    r"顯著性?\s*(為|是|:|：|=)?\s*[\"「]?(false|False|否)|(並非|而非|不是|不屬於|並未|非)\s*顯著(異常|變化|差異)?|"
                    r"非異常|正常波動|波動範圍內|正常範圍|並非異常|無異常|不屬於異常")
SIG_YES = re.compile(r"顯著|異常")
NORMAL = re.compile(r"正常波動|波動範圍內|正常範圍")
CAUSES = re.compile(r"競爭|競品|對手|天氣|氣候|颱風|下雨|疫情|景氣|經濟|政策|法規|罷工|缺貨|庫存不足|供應鏈|物流|"
                    r"行銷活動|廣告|節慶|消費者偏好|消費習慣|需求下降|需求減少|市場飽和|匯率|通膨|人力|員工")
HEDGE = re.compile(r"而非|並非|不是|無法|未顯示|沒有顯示|未包含|不包含|未提供|沒有提供|並未|不能|需|可能|未必|是否|不確定|無從|推測|待|確認|假設|若|如果|或許|也許|資料沒有|資料不含|資料無|不代表|難以|不一定")
SENTENCE = re.compile(r"[。；;！？!?\n]")
CLAUSE = re.compile(r"，|(?<!\d),|,(?!\d)")
MARKDOWN = re.compile(r"\*\*|__|`|^#+\s*|^\s*[-*>]\s+|^\s*\d+\.\s+", re.M)


def sentences(text: str) -> list[list[str]]:
    """Sentences, each split into clauses."""
    out = []
    for sent in SENTENCE.split(MARKDOWN.sub("", text)):
        parts = [c.strip() for c in CLAUSE.split(sent) if c.strip()]
        if parts:
            out.append(parts)
    return out


DATE_TOKEN = re.compile(r"(?<!\d)(\d{4})\s*[-/年.]\s*(\d{1,2})(?:\s*[-/月.]\s*(\d{1,2}))?(?!\d)")


def date_key(m: re.Match) -> str:
    return f"{m[1]}-{int(m[2]):02d}" + (f"-{int(m[3]):02d}" if m[3] else "")


def quoted(text: str, pos: int) -> bool:
    """Inside 「…」 or quotes: a term being named, not a claim."""
    return text[:pos].count("「") > text[:pos].count("」") or text[:pos].count('"') % 2 == 1


def clauses(text: str) -> list[str]:
    return [c for sent in sentences(text) for c in sent]


@dataclass
class Mention:
    start: int
    end: int
    name: str
    kind: str        # "member" | "metric"
    dim: str = ""
    alias: bool = False  # everyday word for a metric ("銷量" → 銷售數量), not a name to look up


def aliases(roles: dict) -> dict[str, str]:
    """Everyday words → the model's metric for that role."""
    words = {"volume": ["銷量", "銷售量", "件數"], "unit_price": ["售價", "單價", "價格"],
             "sales": ["業績", "營收", "營業額"], "orders": ["來客數", "來客", "客數", "訂單量"],
             "discount_rate": ["折扣"], "margin_rate": ["毛利率", "利潤率"]}
    return {w: roles[r] for r, ws in words.items() if isinstance(roles.get(r), str) for w in ws}


class Vocabulary:
    """Metric names and dimension members, matched longest-first (latin names on word boundaries)."""

    def __init__(self, metrics: list[str], members: dict[str, list[str]], alias: dict[str, str] | None = None):
        self.alias = {w: m for w, m in (alias or {}).items() if m in metrics and w not in metrics}
        names = [(m, "metric", "") for m in metrics] + [(w, "metric", "") for w in self.alias]
        # a member spelled like (part of) a dimension label (channel "門市" vs "門市名稱") is ambiguous: skip it
        names += [(m, "member", dim) for dim, ms in members.items() for m in ms
                  if m and not any(m in label for label in members) and m not in metrics]
        names.sort(key=lambda x: -len(x[0]))
        # keyed without spaces: "3C 配件" and "3C配件", "Asia  Pacific" and "Asia Pacific" are the same name
        self.kind = {squash(n): (n, k, d) for n, k, d in reversed(names)}
        parts = []
        for n, _, _ in names:
            p = spaced(n)
            if re.match(r"[A-Za-z0-9]", n[0]):
                p = r"(?<![A-Za-z0-9])" + p
            if re.match(r"[A-Za-z0-9]", n[-1]):
                p += r"(?![A-Za-z0-9])"
            parts.append(p)
        self.pattern = re.compile("|".join(parts)) if parts else None

    def find(self, text: str) -> list[Mention]:
        if not self.pattern:
            return []
        out = []
        for m in self.pattern.finditer(text):
            word, kind, dim = self.kind[squash(m.group())]
            out.append(Mention(m.start(), m.end(), self.alias.get(word, word), kind, dim, word in self.alias))
        return out


def squash(name: str) -> str:
    return re.sub(r"\s+", "", name)


def spaced(name: str) -> str:
    """Regex for a name that tolerates spaces between latin and CJK parts and around existing spaces."""
    out = ""
    for i, ch in enumerate(name):
        if ch.isspace():
            if not out.endswith(r"\s*"):
                out += r"\s*"
            continue
        if i and not name[i - 1].isspace() and name[i - 1].isascii() != ch.isascii():
            out += r"\s?"
        out += re.escape(ch)
    return out


# ------------------------------------------------------------------------------------ evidence
CONTEXT_KEYS = ("範圍", "問題", "指標", "維度")


def units(obj, ctx: tuple = ()) -> list[tuple[str, str]]:
    """Every leaf of a tool result with the labels around it: (text, context)."""
    out = []
    if isinstance(obj, dict):
        local = ctx + tuple(v for k in CONTEXT_KEYS if isinstance(v := obj.get(k), str))
        for k, v in obj.items():
            if not str(k).startswith("_"):
                out += units(v, local + (str(k),))
    elif isinstance(obj, list):
        for v in obj:
            out += units(v, ctx)
    elif obj is not None:
        out.append((str(obj), " ".join(ctx)))
    return out


@dataclass
class Evidence:
    texts: list[str]                                         # what the model saw (+ question, context)
    facts: list[dict] = field(default_factory=list)          # structured claims from the tools
    keys: list[list[str]] = field(default_factory=list)      # members the answer must mention
    main_scopes: list[set] = field(default_factory=list)     # scopes the questions were about
    main_metric: str | None = None                           # the metric the question was about

    def __post_init__(self):
        self.numbers = evidence_set(*self.texts)
        self.blob = "\n".join(self.texts)
        self.tool_blob = "\n".join(t for t in self.texts if t.startswith("{"))  # tool results only
        self.dates = {date_key(m) for t in self.texts for m in DATE_TOKEN.finditer(t)}
        self.units = []
        for t in self.texts:
            try:
                obj = json.loads(t)
            except (json.JSONDecodeError, TypeError):
                self.units.append((t, ""))
                continue
            for text, ctx in units(obj):
                self.units.append((text, ctx))
        self.unit_numbers = [evidence_set(text) for text, _ in self.units]


def split_result(text: str) -> tuple[str, list[dict], list[str]]:
    """Tool result → (what the model sees, facts, key members)."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text, [], []
    if not isinstance(obj, dict):
        return text, [], []
    facts, key = obj.pop("_facts", []), obj.pop("_key", [])
    return json.dumps(obj, ensure_ascii=False), facts, key


# ------------------------------------------------------------------------------------ checker
@dataclass
class Report:
    checked: dict[str, int] = field(default_factory=dict)
    issues: dict[str, list[str]] = field(default_factory=dict)
    where: list[tuple[str, str, str]] = field(default_factory=list)  # (check, issue, clause) for debugging

    def add(self, check: str, issue: str | None = None, where: str = "") -> None:
        self.checked[check] = self.checked.get(check, 0) + 1
        self.issues.setdefault(check, [])
        if issue and issue not in self.issues[check]:
            self.issues[check].append(issue)
            self.where.append((check, issue, where))

    @property
    def ok(self) -> bool:
        return not any(self.issues.values())

    def all_issues(self) -> list[str]:
        return [f"[{k}] {i}" for k, v in self.issues.items() for i in v]

    def summary(self) -> dict:
        return {k: {"checked": self.checked.get(k, 0), "issues": self.issues.get(k, [])} for k in CHECKS}


CHECKS = ("日期", "範圍", "數字", "名稱", "對應", "方向", "顯著性", "原因", "重點")


class Checker:
    def __init__(self, vocab: Vocabulary):
        self.vocab = vocab

    def check(self, answer: str, ev: Evidence) -> Report:
        r = Report()
        for check in CHECKS:
            r.checked.setdefault(check, 0)
            r.issues.setdefault(check, [])
        self.check_numbers(answer, ev, r)
        self.check_names(answer, ev, r)
        for sent in sentences(answer):
            carry: list[Mention] = []  # a clause without names continues the previous one ("…，顯著異常")
            carry_metric: list[Mention] = []
            for clause in sent:
                if REPORTED.search(clause):
                    continue  # restating the question ("問題說下降") is not a claim
                own = self.vocab.find(clause)
                if any(x.kind == "member" for x in own):
                    carry = own
                    ctx = own
                else:  # inherit the previous clause's names; its metric only if this clause names none
                    inherited = [x for x in carry if x.kind == "member" or not any(o.kind == "metric" for o in own)]
                    ctx = [Mention(-1, -1, x.name, x.kind, x.dim) for x in inherited] + own
                self.check_binding(clause, own, ev, r)
                # direction: only a metric named in this clause ("同時銷量下降" is not about the previous metric)
                metrics = [x for x in own if x.kind == "metric"]
                d = DIRECTION.search(clause)
                if not metrics and d and d.start() <= 6 and not SUBJECT.search(clause[:d.start()]):
                    metrics = [Mention(-1, -1, x.name, x.kind) for x in carry_metric[-1:]]
                self.check_direction(clause, [x for x in ctx if x.kind == "member"] + metrics, ev, r)
                self.check_significance(clause, ctx, ev, r)
                self.check_cause(clause, ev, r)
                carry_metric = [x for x in own if x.kind == "metric"] or carry_metric
        self.check_key(answer, ev, r)
        return r

    # --- 數字 / 名稱
    def check_numbers(self, answer: str, ev: Evidence, r: Report) -> None:
        # dates first, as whole dates ("2015-10-11" is not three numbers that each happen to appear somewhere)
        for m in DATE_TOKEN.finditer(answer):
            r.add("數字", None if date_key(m) in ev.dates else f"日期「{m.group()}」在工具結果中找不到")
        rest = DATE_TOKEN.sub(" ", answer)
        bad = set(ungrounded(rest, ev.numbers))
        for n in numbers(rest):
            r.add("數字", f"「{n}」在工具結果中找不到" if n in bad else None)

    def check_names(self, answer: str, ev: Evidence, r: Report) -> None:
        for m in self.vocab.find(MARKDOWN.sub("", answer)):
            if m.alias:
                continue
            found = re.search(re.escape(m.name), ev.blob)
            r.add("名稱", None if found else f"「{m.name}」沒有出現在任何工具結果中")

    # --- 對應: number ↔ the things named right before it
    def check_binding(self, clause: str, mentions: list[Mention], ev: Evidence, r: Report) -> None:
        prev_end = 0
        for m in NUMBER.finditer(clause):
            if any(x.start <= m.start() < x.end for x in mentions):
                continue  # digits inside a name ("面膜 5 入")
            n = normalize(m.group())
            between = [x for x in mentions if prev_end <= x.start and x.end <= m.start()]
            prev_end = m.end()
            if not between or not grounded(n, ev.numbers) or re.fullmatch(r"\d", n):
                continue
            dims = [x.dim for x in between if x.kind == "member"]
            if len(set(dims)) < len(dims):
                continue  # a list of members ("A、B、C 均…") — the number cannot belong to all of them
            # each required name may be satisfied by any of its spellings (metric name or the everyday word used)
            metric = [x for x in between if x.kind == "metric"][-1:]
            need = [{x.name} for x in between if x.kind == "member"]
            need += [{x.name, clause[x.start:x.end]} | {w for w, m in self.vocab.alias.items() if m == x.name}
                     for x in metric]
            ok = any(grounded(n, nums) and all(any(w in f"{text} {ctx}" for w in alts) for alts in need)
                     for (text, ctx), nums in zip(ev.units, ev.unit_numbers))
            need = {min(alts, key=len) for alts in need}
            r.add("對應", None if ok else f"「{'、'.join(sorted(need))} … {n}」：工具結果中 {n} 不是這個範圍／指標的數字", clause)

    # --- facts about a clause
    def candidates(self, members: set[str], metric: str | None, ev: Evidence) -> list[dict]:
        """Facts about exactly these members (the question's own scope may be implied) and this metric."""
        out = []
        for f in ev.facts:
            fm = set(f["scope"].values())
            if metric and f["metric"] != metric:
                continue
            if not members <= fm:
                continue
            rest = fm - members
            if rest and not any(rest <= s for s in ev.main_scopes):
                continue
            if not members and fm not in ev.main_scopes:
                continue
            out.append(f)
        return out

    def targets(self, mentions: list[Mention], before: int | None = None) -> list[set[str]]:
        """Member sets a claim is about: all members in the clause, or each one when they are listed."""
        ms = [x for x in mentions if x.kind == "member" and (before is None or x.end <= before)]
        if not ms:
            return [set()]
        by_dim: dict[str, list[str]] = {}
        for x in ms:
            if x.name not in by_dim.setdefault(x.dim, []):
                by_dim[x.dim].append(x.name)
        fixed = {v[0] for v in by_dim.values() if len(v) == 1}
        lists = [v for v in by_dim.values() if len(v) > 1]
        if len(lists) == 1:  # a list of one dimension ("LATAM × Art、LATAM × Paper"): each with the rest
            return [fixed | {name} for name in lists[0]]
        if lists:
            return [{x} for v in by_dim.values() for x in v]
        return [fixed]

    def resolve(self, targets: list[set[str]], metric: str | None, ev: Evidence, usable) -> list[tuple[set, list]]:
        """(members, facts) for each target; a combination nothing is known about ("A 與 B 顯著", A and B in
        different dimensions) is checked member by member."""
        out = []
        for members in targets:
            cands = [f for f in self.candidates(members, metric, ev) if usable(f)]
            if cands:
                out.append((members, cands))
            elif len(members) > 1:
                for one in members:
                    cands = [f for f in self.candidates({one}, metric, ev) if usable(f)]
                    if cands:
                        out.append(({one}, cands))
        return out

    def metric_of(self, mentions: list[Mention], pos: int) -> str | None:
        before = [x.name for x in mentions if x.kind == "metric" and x.end <= pos]
        return before[-1] if before else None

    # --- 方向
    def check_direction(self, clause: str, mentions: list[Mention], ev: Evidence, r: Report) -> None:
        for m in DIRECTION.finditer(clause):
            if NEGATION.search(clause[max(0, m.start() - 4):m.start()]) or quoted(clause, m.start()):
                continue
            sign = 1 if m.group("up") else -1
            metric = self.metric_of(mentions, m.start())
            if not metric:
                continue  # "銷量下降" without a metric name: we cannot tell what it refers to
            last = max((x.end for x in mentions if x.kind == "metric" and x.end <= m.start()), default=0)
            if SUBJECT.search(clause[last:m.start()]) or len(DATE_TOKEN.sub("", clause[last:m.start()])) > 20:
                continue  # "低毛利率商品占比提高": the direction is about something else
            for members, cands in self.resolve(self.targets(mentions, m.start()), metric, ev,
                                               lambda f: bool(f["change"])):
                # raw or adjusted (vs last year / vs the total) change may both be what the answer describes
                agree = any((v > 0) == (sign > 0) for f in cands for v in (f["change"], f.get("adjusted")) if v)
                word = m.group()
                label = " × ".join(sorted(members)) or "本範圍"
                r.add("方向", None if agree else
                      f"「{label} {metric}{word}」與工具結果的方向相反（{cands[0]['metric']} 變化為"
                      f"{'正' if cands[0]['change'] > 0 else '負'}）", clause)

    def main_metric(self, ev: Evidence) -> str | None:
        return ev.main_metric

    # --- 顯著性
    def check_significance(self, clause: str, mentions: list[Mention], ev: Evidence, r: Report) -> None:
        no = next((m for m in SIG_NO.finditer(clause)
                   if not NEGATION.search(clause[max(0, m.start() - 4):m.start()])), None)  # "並非正常波動"
        yes = None
        for m in SIG_YES.finditer(SIG_NO.sub(lambda x: "＿" * len(x.group()), clause)):
            if not NEGATION.search(clause[max(0, m.start() - 4):m.start()]) and not quoted(clause, m.start()):
                yes = m
                break
        if not (yes or no) or (yes and no):
            return
        claim = bool(yes)
        # three levels, as the tool reads them: 顯著異常 / 偏大但未達顯著 / 正常波動
        normal = bool(no) and bool(NORMAL.search(no.group()))
        metric = self.metric_of(mentions, len(clause)) or self.main_metric(ev)
        for members, cands in self.resolve(self.targets(mentions), metric, ev,
                                           lambda f: f.get("significant") is not None):
            agree = any(bool(f["significant"]) == claim and not (normal and f["verdict"].startswith("變化偏大"))
                        for f in cands)
            label = " × ".join(sorted(members)) or "本範圍"
            said = "顯著異常" if claim else ("正常波動" if normal else "未達顯著")
            r.add("顯著性", None if agree else
                  f"「{label}」的 {metric} 被說成{said}，但工具判讀是「{cands[0]['verdict'][:24]}」", clause)

    # --- 原因
    def check_cause(self, clause: str, ev: Evidence, r: Report) -> None:
        for m in CAUSES.finditer(clause):
            if HEDGE.search(clause) or m.group() in ev.tool_blob:
                r.add("原因")
            else:
                r.add("原因", f"「{clause[:40]}」把資料無法證明的原因（{m.group()}）當成事實")

    # --- 重點
    def check_key(self, answer: str, ev: Evidence, r: Report) -> None:
        for key in ev.keys:
            if not key:
                continue
            ok = any(k in answer for k in key)
            r.add("重點", None if ok else f"沒有提到工具判定為顯著異常、能解釋這次變化的「{'、'.join(key)}」")


def feedback(report: Report, limit: int = 5) -> str:
    issues = report.all_issues()
    return ("自動查核發現以下問題，請只依工具結果修正後重寫完整答案：\n- " + "\n- ".join(issues[:limit])
            + "\n（數字與名稱要原文照抄工具結果；方向、是否顯著要跟工具的「判讀」一致；"
              "資料無法證明的原因只能寫成「可能」並說明需業務確認；結論要提到「重點」中顯著異常的部分。）")
