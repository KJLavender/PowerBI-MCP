"""Several Power BI reports in one graph: what each one reads, how they connect, where they agree and disagree.

Everything here is read from the reports themselves (TMDL model, Power Query, report layout) and, for keys that
join different systems, from the source data. Nothing is guessed from names:

  - lineage: every table / column is traced to the physical source it reads (an Oracle table on a server and
    schema, a sheet of a workbook), so two reports that rename FACT_SALES[SALES_AMOUNT] to 銷售[實收金額] still
    meet on the same column
  - measures: each measure is expanded (measure references, CALCULATE filters, time intelligence) into arithmetic
    over aggregates of physical columns; two measures mean the same when the two expressions agree for random
    values of those aggregates, whatever their names or however the DAX is written
  - same name, different meaning: reported with both definitions side by side
  - keys between systems (an Excel code list against an Oracle table): compared value by value, listing what only
    one side has and near misses (case, spaces); relationships inside one database: rows whose key has no match
  - the author's emphasis (intent.py) hangs off the measure or column it is about

    python -m analysis catalog            # all reports: powerbi/ and datasets/*/powerbi
    python -m analysis catalog --no-data  # without reading source data (no key comparison)
"""
import heapq
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote as url_unquote

import yaml

from . import intent, semantic
from .dataset import ROOT
from .semantic import SemanticModel, Table, Unsupported, tokenize, unquote

DISTINCT_LIMIT = 5000     # columns with more distinct values are not compared (ids, free text)
MIN_SHARED = 3            # values two columns from different systems must share to count as a link
MIN_CONTAINMENT = 0.5     # share of the smaller column's values found in the other one
# path costs: links of meaning are cheaper than sharing a column or a table
EDGE_COST = {"同義": 0.5, "引用": 1.0, "模型關聯": 1.0, "跨系統代碼對應": 1.0, "跨報表值重疊": 1.5, "同名但定義不同": 1.5, "同名（不同系統": 3.0,
             "計算自": 1.5, "來自": 1.0, "欄位": 1.0, "量值": 1.5, "讀取": 2.0, "凸顯": 1.0, "包含資料表": 3.0}
KEY = re.compile(r"(?:oracle|sql|excel|csv|pbix):[^\s,()|\[\]]+")


# ------------------------------------------------------------------------------------------- reports
@dataclass
class Report:
    name: str             # dataset name (PBI_DATASET)
    title: str            # report name in Power BI
    pbip: Path
    model: SemanticModel
    emphasis: list = field(default_factory=list)
    source: dict = field(default_factory=dict)   # the dataset's analysis source (source: pbix → data in the file)


def discover() -> list[tuple[str, Path]]:
    """Every dataset: the retail report, the examples in datasets/ and what was uploaded to workspace/."""
    out = [("retail", ROOT / "powerbi")]
    for base in (ROOT / "datasets", ROOT / "workspace"):
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if any((d / "powerbi").glob("*.SemanticModel")) and any((d / "powerbi").glob("*.Report")):
                out.append((d.name, d / "powerbi"))
    return out


def load_reports(names: list[str] | None = None) -> list[Report]:
    out = []
    for name, pbip in discover():
        if names is not None and name not in names:
            continue
        report_dir = next(pbip.glob("*.Report"))
        model = semantic.load(pbip)
        percent = {m.name for m in model.measures.values() if "%" in m.format_string}
        config = pbip.parent / "analysis.yaml" if name != "retail" else ROOT / "analysis" / "config.yaml"
        source = (yaml.safe_load(config.read_text(encoding="utf-8")) or {}).get("source") or {} \
            if config.exists() else {}
        out.append(Report(name, report_dir.name[:-len(".Report")], pbip, model, intent.extract(report_dir, percent),
                          source))
    return out


# ------------------------------------------------------------------------------------------- lineage
# Physical keys, every part URL-encoded so a key never contains spaces, slashes or brackets:
#   table   oracle:<server>/<SCHEMA>/<TABLE>   excel:<file>//<sheet or table>   csv:<file>//
#   column  <table key>/<column>
LABELS: dict[str, str] = {}   # table key → readable name with the original case


def _q(s: str) -> str:
    return quote(s, safe="")


def source_id(t: Table) -> str | None:
    """The physical table a model table reads."""
    s = t.source
    if s.get("kind") == "oracle" and t.source_table:
        server, schema = (s.get("server") or "?").strip().lower(), (s.get("schema") or "?").upper()
        sid = f"oracle:{_q(server)}/{_q(schema)}/{_q(t.source_table.upper())}"
        LABELS.setdefault(sid, f"Oracle {schema}.{t.source_table.upper()}")
        return sid
    if s.get("kind") == "sql" and t.source_table:
        server = (s.get("server") or "?").strip().lower()
        db = f"{s.get('database') or '?'}.{s.get('schema') or 'dbo'}"
        sid = f"sql:{_q(server)}/{_q(db.upper())}/{_q(t.source_table.upper())}"
        LABELS.setdefault(sid, f"SQL Server {db}.{t.source_table}")
        return sid
    if s.get("kind") in ("excel", "csv") and s.get("file"):
        path = str(s["file"]).replace("\\", "/")
        sid = f"{s['kind']}:{_q(path.lower())}//{_q(t.source_table or '')}"
        name = path.rsplit("/", 1)[-1]
        LABELS.setdefault(sid, f"{'Excel' if s['kind'] == 'excel' else 'CSV'} {name}"
                          + (f"／{t.source_table}" if t.source_table else ""))
        return sid
    return None


def split_key(key: str) -> tuple[str, str | None]:
    """Physical key → (table key, column or None)."""
    kind, rest = key.split(":", 1)
    parts = rest.split("/")
    if len(parts) == 4:
        return f"{kind}:{'/'.join(parts[:3])}", url_unquote(parts[3])
    return key, None


def system_of(table_key: str) -> str:
    """The database (server + schema) or file a table key belongs to."""
    return table_key.rsplit("/", 1)[0]


def source_label(table_key: str) -> str:
    return LABELS.get(table_key, table_key)


def column_source(t: Table, column: str) -> str | None:
    """Physical column behind a model column (through model renames and Power Query renames)."""
    sid = source_id(t)
    col = t.columns.get(column)
    if not sid or col is None or col.calculated:  # a calculated column comes from DAX, not from the source
        return None
    src = col.source_column or column
    for old, new in t.source.get("renames", []):
        if new.replace('""', '"') == src:
            src = old.replace('""', '"')
    return f"{sid}/{_q(src.upper() if sid.startswith('oracle:') else src)}"


def col_key(report: "Report", table: str, column: str) -> str | None:
    """The physical column; for a report imported from a .pbix whose source this machine does not know
    (a database on the author's network …), the column as stored in that .pbix."""
    key = column_source(report.model.tables[table], column)
    if key is None and report.source.get("kind") == "pbix" and column in report.model.tables[table].columns:
        sid = f"pbix:{_q(report.name)}/{_q('')}/{_q(table)}"
        LABELS.setdefault(sid, f"{report.title}.pbix／{table}")
        key = f"{sid}/{_q(column)}"
    return key


def short(key: str) -> str:
    """Physical key → readable: FACT_SALES.SALES_AMOUNT, GlobalSuperstore.xlsx／Orders.Sales"""
    table, column = split_key(key)
    label = source_label(table).split(" ", 1)[-1]
    if table.startswith("oracle:"):
        label = label.split(".", 1)[-1]
    return f"{label}.{column}" if column else label


# ------------------------------------------------------------------------------------------- measures
# expression tree: ("num", v) | ("atom", key) | ("neg", x) | (op, a, b) with op in + - * /
@dataclass
class Canonical:
    report: str
    measure: str
    table: str
    expression: str            # DAX as written
    tree: tuple | None = None
    reason: str = ""           # why it could not be expanded

    @property
    def atoms(self) -> set[str]:
        return _atoms(self.tree) if self.tree else set()

    @property
    def systems(self) -> set[str]:
        return {system_of(split_key(k)[0]) for a in self.atoms for k in KEY.findall(a)}

    def readable(self) -> str:
        return _render(self.tree) if self.tree else f"（無法展開：{self.reason}）"


def _atoms(tree) -> set[str]:
    if tree[0] == "atom":
        return {tree[1]}
    if tree[0] == "num":
        return set()
    return set().union(*(_atoms(x) for x in tree[1:]))


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def _render(tree, parent: str = "", names: dict | None = None) -> str:
    """names: aggregate → measure name of one report, to write the formula in that report's own terms."""
    kind = tree[0]
    if kind == "num":
        return _num(tree[1])
    if kind == "atom":
        if names and tree[1] in names:
            return f"「{names[tree[1]]}」"
        agg, _, mods = tree[1].partition(" | ")
        return KEY.sub(lambda m: short(m.group(0)), agg) + (f"［{mods}］" if mods else "")
    if kind == "neg":
        return f"-{_render(tree[1], '*', names)}"
    a, b = _render(tree[1], kind, names), _render(tree[2], kind + "r", names)
    text = f"{a} ÷ {b}" if kind == "/" else f"{a} {'×' if kind == '*' else kind} {b}"
    tight = {"-r": kind in "+-", "*": kind in "+-", "*r": kind in "+-", "/": kind in "+-",
             "/r": kind in "+-*/"}.get(parent, False)
    return f"({text})" if tight else text


AGGREGATES = {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "DISTINCTCOUNT", "COUNTBLANK"}
ITERATORS = {"SUMX", "AVERAGEX", "MINX", "MAXX"}


class Expander:
    """DAX measure → expression tree over physical aggregates (the subset report measures commonly use)."""

    def __init__(self, report: Report):
        self.r = report
        self.model = report.model
        self.trees: dict[str, tuple] = {}
        self.stack: list[str] = []
        self.tokens, self.i = [], 0

    def measure(self, name: str) -> tuple:
        if name in self.trees:
            return self.trees[name]
        if name in self.stack:
            raise Unsupported(f"量值循環引用 {name}")
        m = self.model.measures.get(name)
        if m is None:
            raise Unsupported(f"找不到量值 {name}")
        self.stack.append(name)
        saved = self.tokens, self.i, getattr(self, "home", None)
        try:
            self.tokens, self.i, self.home = tokenize(m.expression), 0, m.table
            tree = self._expr()
            if self.i != len(self.tokens):
                raise Unsupported(f"無法解讀 {self.tokens[self.i][1]!r} 之後的內容")
        finally:
            self.tokens, self.i, self.home = saved
            self.stack.pop()
        self.trees[name] = tree
        return tree

    def expression(self, dax: str, home: str | None = None) -> tuple:
        """Any DAX expression (not a stored measure), e.g. the aggregate behind an analysis base."""
        saved = self.tokens, self.i
        try:
            self.tokens, self.i = tokenize(dax), 0
            tree = self._expr()
            if self.i != len(self.tokens):
                raise Unsupported(f"無法解讀 {self.tokens[self.i][1]!r} 之後的內容")
            return tree
        finally:
            self.tokens, self.i = saved

    # ---- tokens
    def _peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def _take(self, kind=None, value=None):
        tok = self._peek()
        if tok[0] is None or (kind and tok[0] != kind) or (value and tok[1].upper() != value):
            raise Unsupported(f"預期 {value or kind}，遇到 {tok[1]!r}")
        self.i += 1
        return tok

    # ---- references
    def _colref(self, text: str) -> str:
        m = re.match(r"('(?:[^']|'')*'|[^\[\s]+)\s*\[([^\]]+)\]", text)
        table, column = unquote(m.group(1)), m.group(2)
        t = self.model.tables.get(table)
        if t is None or column not in t.columns:
            raise Unsupported(f"找不到欄位 {text}")
        return column_source(t, column) or f"model:{_q(self.r.name)}/{_q(table)}/{_q(column)}"

    def _tableref(self, text: str) -> str:
        table = unquote(text)
        t = self.model.tables.get(table)
        if t is None:
            raise Unsupported(f"找不到資料表 {text}")
        return source_id(t) or f"model:{_q(self.r.name)}/{_q(table)}"

    # ---- grammar
    def _expr(self):
        out = self._term()
        while self._peek() in (("op", "+"), ("op", "-")):
            op = self._take()[1]
            out = (op, out, self._term())
        return out

    def _term(self):
        out = self._unary()
        while self._peek() in (("op", "*"), ("op", "/")):
            op = self._take()[1]
            out = (op, out, self._unary())
        return out

    def _unary(self):
        if self._peek() == ("op", "-"):
            self.i += 1
            return ("neg", self._unary())
        return self._factor()

    def _factor(self):
        kind, val = self._peek()
        if kind == "num":
            self.i += 1
            return ("num", float(val))
        if (kind, val) == ("op", "("):
            self.i += 1
            inner = self._expr()
            self._take("op", ")")
            return inner
        if kind == "mref":
            self.i += 1
            return self.measure(val[1:-1])
        if kind == "func":
            return self._func(val.upper())
        raise Unsupported(f"不支援 {val!r}")

    def _func(self, fn: str):
        self.i += 1
        self._take("op", "(")
        if fn in AGGREGATES:
            kind, ref = self._take()
            home = getattr(self, "home", None)
            if kind == "mref" and home and ref[1:-1] in self.model.tables[home].columns:
                ref = f"'{home}'{ref}"  # [Column] of the measure's own table
            elif kind != "colref":
                raise Unsupported(f"預期欄位，遇到 {ref!r}")
            self._take("op", ")")
            return ("atom", f"{fn}({self._colref(ref)})")
        if fn == "COUNTROWS":
            table = self._take()[1]
            self._take("op", ")")
            return ("atom", f"COUNTROWS({self._tableref(table)})")
        if fn in ITERATORS:
            table = self._tableref(self._take()[1])
            self._take("op", ",")
            parts, depth = [], 0
            while True:  # row expression up to the closing parenthesis
                k, v = self._take()
                if (k, v) == ("op", "("):
                    depth += 1
                elif (k, v) == ("op", ")"):
                    if depth == 0:
                        break
                    depth -= 1
                parts.append(self._colref(v) if k == "colref" else v)
            return ("atom", f"{fn}({table}, {' '.join(parts)})")
        if fn == "IF":  # IF(x <> 0, y / x, BLANK()) is DIVIDE(y, x)
            cond = self._expr()
            op = self._take("cmp")[1]
            zero = float(self._take("num")[1])
            self._take("op", ",")
            then = self._expr()
            if self._peek() == ("op", ","):
                self.i += 1
                if self._take("func")[1].upper() != "BLANK":
                    raise Unsupported("IF 只接受「分母不為 0 才相除」的寫法")
                self._take("op", "(")
                self._take("op", ")")
            self._take("op", ")")
            if op != "<>" or zero != 0 or then[0] != "/" or then[2] != cond:
                raise Unsupported("IF 只接受「分母不為 0 才相除」的寫法：IF(x <> 0, y / x, BLANK())")
            return then
        if fn == "DIVIDE":
            num = self._expr()
            self._take("op", ",")
            den = self._expr()
            if self._peek() == ("op", ","):  # alternate result: only matters when the denominator is 0
                self.i += 1
                self._expr()
            self._take("op", ")")
            return ("/", num, den)
        if fn == "CALCULATE":
            inner = self._expr()
            mods = []
            while self._peek() == ("op", ","):
                self.i += 1
                mods.append(self._modifier())
            self._take("op", ")")
            return _apply(inner, mods)
        if fn in ("TOTALMTD", "TOTALQTD", "TOTALYTD"):
            inner = self._expr()
            self._take("op", ",")
            date = self._colref(self._take("colref")[1])
            self._take("op", ")")
            return _apply(inner, [f"{fn[5:]} 累計（{short(date)}）"])
        raise Unsupported(f"函數 {fn} 尚未支援")

    def _modifier(self) -> str:
        """One CALCULATE filter argument → canonical text."""
        kind, val = self._peek()
        if kind == "func":
            fn = val.upper()
            self.i += 1
            self._take("op", "(")
            if fn == "KEEPFILTERS":
                inner = self._modifier()
                self._take("op", ")")
                return inner
            if fn == "DATEADD":
                date = self._colref(self._take("colref")[1])
                self._take("op", ",")
                sign = -1 if self._peek() == ("op", "-") else 1
                if sign < 0:
                    self.i += 1
                n = int(float(self._take("num")[1])) * sign
                self._take("op", ",")
                unit = self._take()[1].upper()
                self._take("op", ")")
                return f"日期位移 {n:+d} {unit}（{short(date)}）"
            if fn == "SAMEPERIODLASTYEAR":  # = DATEADD(dates, -1, YEAR)
                date = self._colref(self._take("colref")[1])
                self._take("op", ")")
                return f"日期位移 -1 YEAR（{short(date)}）"
            if fn in ("ALL", "REMOVEFILTERS"):
                refs = []
                while self._peek() != ("op", ")"):
                    k, v = self._take()
                    if k == "colref":
                        refs.append(short(self._colref(v)))
                    elif k == "ident":
                        refs.append(short(self._tableref(v)))
                self._take("op", ")")
                return f"移除篩選（{'、'.join(sorted(refs))}）"
            raise Unsupported(f"CALCULATE 篩選 {fn} 尚未支援")
        if kind == "colref":
            col = short(self._colref(val))
            self.i += 1
            nk, nv = self._peek()
            if nk == "ident" and nv.upper() == "IN":
                self.i += 1
                self._take("op", "{")
                values = [self._literal_text()]
                while self._peek() == ("op", ","):
                    self.i += 1
                    values.append(self._literal_text())
                self._take("op", "}")
                return f"{col} IN {{{', '.join(sorted(values))}}}"
            if nk != "cmp":
                raise Unsupported(f"CALCULATE 篩選 {val} 需要比較運算")
            self.i += 1
            return f"{col} {'=' if nv == '==' else nv} {self._literal_text()}"
        raise Unsupported(f"CALCULATE 篩選 {val!r} 尚未支援")

    def _literal_text(self) -> str:
        kind, val = self._take()
        if kind == "str":
            return '"' + val[1:-1].replace('""', '"') + '"'
        if kind == "num":
            return _num(float(val))
        if (kind, val) == ("op", "-"):
            return "-" + _num(float(self._take("num")[1]))
        raise Unsupported(f"預期常數，遇到 {val!r}")


def _apply(tree, mods: list[str]):
    """CALCULATE modifiers onto every aggregate of the tree."""
    if not mods:
        return tree
    if tree[0] == "atom":
        agg, _, old = tree[1].partition(" | ")
        return ("atom", f"{agg} | {'；'.join(sorted(set(filter(None, old.split('；'))) | set(mods)))}")
    if tree[0] == "num":
        return tree
    return (tree[0], *(_apply(x, mods) for x in tree[1:]))


def expand(report: Report) -> dict[str, Canonical]:
    ex = Expander(report)
    out = {}
    for name, m in report.model.measures.items():
        c = Canonical(report.name, name, m.table, m.expression)
        try:
            c.tree = ex.measure(name)
        except (Unsupported, KeyError, ValueError, AttributeError, IndexError) as e:
            c.reason = str(e) or type(e).__name__
        out[name] = c
    return out


def _value(tree, values: dict) -> float:
    kind = tree[0]
    if kind == "num":
        return tree[1]
    if kind == "atom":
        return values[tree[1]]
    if kind == "neg":
        return -_value(tree[1], values)
    a, b = _value(tree[1], values), _value(tree[2], values)
    if kind == "/":
        return a / b if b else math.nan
    return a + b if kind == "+" else a - b if kind == "-" else a * b


def same_meaning(a: Canonical, b: Canonical, trials: int = 6) -> bool:
    """Two expanded measures agree for random positive values of their aggregates (each aggregate gets the same
    value in both): equal as formulas, however they are written."""
    if not (a.tree and b.tree):
        return False
    keys = sorted(a.atoms | b.atoms)
    for t in range(trials):
        values = {k: random.Random(f"{t}:{k}").uniform(1, 1000) for k in keys}
        x, y = _value(a.tree, values), _value(b.tree, values)
        if math.isnan(x) or math.isnan(y) or abs(x - y) > 1e-9 * max(1.0, abs(x), abs(y)):
            return False
    return True


# ------------------------------------------------------------------------------------------- source data
class Values:
    """Distinct values of physical columns, read once per column (Oracle through the simulator connection,
    workbooks the way their Power Query reads them)."""

    def __init__(self):
        self.cache: dict[str, set[str] | None] = {}
        self.notes: list[str] = []
        self._oracle = None
        self._frames: dict[str, object] = {}

    def get(self, report: Report, table: str, column: str) -> set[str] | None:
        key = col_key(report, table, column)
        if key is None:
            return None
        cache_key = (key, report.source.get("file")) if report.source.get("kind") == "pbix" else key
        if cache_key not in self.cache:
            try:
                self.cache[cache_key] = self._from_pbix(report, table, column) \
                    if report.source.get("kind") == "pbix" else self._read(report.model.tables[table], column, key)
            except Exception as e:  # source unreachable / file missing: compare what can be read
                self.notes.append(f"讀不到 {short(key)}：{e}")
                self.cache[cache_key] = None
        return self.cache[cache_key]

    def _from_pbix(self, report: Report, table: str, column: str) -> set[str] | None:
        """The values the report imported, read from its .pbix (the author's sources are not reachable here)."""
        path = report.source["file"]
        if path not in self._frames:
            from .pbix import open_pbix
            self._frames[path] = open_pbix(Path(path))
        df = self._frames[path].get_table(table)
        if column not in df.columns:
            return None
        values = df[column].dropna().unique()
        return None if len(values) > DISTINCT_LIMIT else {_norm(v) for v in values}

    def _read(self, t: Table, column: str, key: str) -> set[str] | None:
        if key.startswith("oracle:"):
            return self._oracle_values(t, key)
        from . import sources
        sid = source_id(t)
        if sid not in self._frames:
            self._frames[sid] = sources.read_file_table(t, None)
        df = self._frames[sid]
        if column not in df.columns:
            return None
        values = df[column].dropna().unique()
        return None if len(values) > DISTINCT_LIMIT else {_norm(v) for v in values}

    def _oracle_values(self, t: Table, key: str) -> set[str] | None:
        import os
        from simulator.db import connect, load_env
        load_env()
        dsn = os.environ.get("ORACLE_DSN", "127.0.0.1:1521/FREEPDB1").lower()
        server = (t.source.get("server") or "").strip().lower()
        if server != dsn:
            raise ConnectionError(f"沒有 {server} 的連線設定（.env 的 ORACLE_DSN 是 {dsn}）")
        if self._oracle is None:
            self._oracle = connect()
        table_key, col = split_key(key)
        schema, table = (url_unquote(p) for p in table_key.split(":", 1)[1].split("/")[1:3])
        cur = self._oracle.cursor()
        cur.execute(f'SELECT DISTINCT "{col}" FROM "{schema}"."{table}" WHERE "{col}" IS NOT NULL '
                    f"FETCH FIRST {DISTINCT_LIMIT + 1} ROWS ONLY")
        rows = cur.fetchall()
        return None if len(rows) > DISTINCT_LIMIT else {_norm(r[0]) for r in rows}


def _norm(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if hasattr(v, "hour") and hasattr(v, "date"):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat()
    return str(v)


def _loose(v: str) -> str:
    return re.sub(r"\s+", "", v).upper()


def _numeric_share(values: set[str]) -> float:
    return sum(1 for v in values if re.fullmatch(r"-?\d+(\.\d+)?", v)) / max(1, len(values))


@dataclass
class KeyMatch:
    kind: str                  # relationship (between systems) | orphans (inside one system) | overlap (between reports)
    #                            | same_file (a column of the same workbook / sheet at two paths)
    left: list[str]            # report:table[column] on each report that has this pair
    right: list[str]
    left_source: str
    right_source: str
    shared: int
    left_only: list[str]
    right_only: list[str]
    near: list[tuple[str, str]]   # (value, value on the other side) equal apart from case / spaces
    left_count: int
    right_count: int
    note: str = ""             # e.g. the same values in another format (Excel text dates, converted by Power Query)

    @property
    def containment(self) -> float:
        return self.shared / max(1, min(self.left_count, self.right_count))


def _as_dates(a: set[str], b: set[str]) -> str:
    """Same values written as text on one side and as dates on the other ("2017 Aug" ↔ 2017-08-01)?"""
    import pandas as pd

    def dates(values) -> dict[str, str] | None:
        """date → the value as written"""
        values = sorted(values)
        try:
            parsed = pd.to_datetime(pd.Series(values), errors="coerce", format="mixed")
        except (ValueError, TypeError):
            return None
        if parsed.isna().mean() > 0.05:
            return None
        return {d.date().isoformat(): v for v, d in zip(values, parsed) if not pd.isna(d)}

    da, db = dates(a), dates(b)
    if not da or not db or set(da) != set(db):
        return ""
    day = min(da)
    sample_a, sample_b = da[day], db[day]
    return f"格式不同但轉成日期後完全一致（「{sample_a}」↔「{sample_b}」）：Power Query 做了型別轉換"


def compare(a: set[str], b: set[str]) -> tuple[int, list[str], list[str], list[tuple[str, str]]]:
    """Shared count, values only in a, only in b, and near misses of those (same apart from case and spaces)."""
    left, right = sorted(a - b), sorted(b - a)
    loose_b, loose_a = {_loose(v): v for v in b}, {_loose(v): v for v in a}
    near = [(v, loose_b[_loose(v)]) for v in left if _loose(v) in loose_b] + \
        [(loose_a[_loose(v)], v) for v in right if _loose(v) in loose_a]
    near = list(dict.fromkeys(near))
    return len(a & b), left, right, near


# ------------------------------------------------------------------------------------------- the catalog
@dataclass
class Conflict:
    name: str
    groups: list[list[Canonical]]   # one list per meaning
    same_system: bool               # the definitions read the same database / file (a real clash)


@dataclass
class Catalog:
    reports: list[Report]
    measures: dict[tuple[str, str], Canonical]
    same: list[list[Canonical]]                       # measures with the same meaning (2+ members)
    conflicts: list[Conflict]                         # same name, different meaning
    shared_sources: dict[str, list[tuple[str, str]]]  # physical table → [(report, model table)]
    keys: list[KeyMatch]
    notes: list[str]
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (from, relation, to)

    def in_terms_of(self, c: Canonical, report: str, partial: bool = False) -> str | None:
        """c's formula written with the measures of another report, when each of its aggregates is one of them
        ('「銷售額」 + 「折扣金額」'); None when some aggregate has no measure there (partial: keep those as they are)."""
        if not c.tree:
            return None
        names = {}
        for x in self.measures.values():
            if x.report == report and x.tree and x.tree[0] == "atom":
                names.setdefault(x.tree[1], x.measure)
        if not partial and not c.atoms <= set(names):
            return None
        return _render(c.tree, names=names)

    def group_of(self, report: str, measure: str) -> list[Canonical]:
        for g in self.same:
            if any(c.report == report and c.measure == measure for c in g):
                return g
        c = self.measures.get((report, measure))
        return [c] if c else []

    # ---- graph queries
    def find(self, text: str, kinds: tuple = ()) -> list[str]:
        """Node ids for a name: '銷售額', 'store_targets:營收', '門市[門市代碼]', 'retail:DIM_STORE[STORE_NAME]'.
        Exact names first (measures before columns and tables), then names containing the text."""
        text = text.strip()
        if text in self.nodes:
            return [text]
        report = ""
        m = re.match(r"([^:：\[\]]+)[:：](.+)$", text)
        if m and (r := self.report(m.group(1))):
            report, text = r.name, m.group(2).strip()
        table = None
        m = re.fullmatch(r"'?(.+?)'?\[(.+)\]", text)
        if m:
            table, text = m.group(1), m.group(2)
        order = {"measure": 0, "column": 1, "table": 2, "source": 3, "source_column": 4}
        pool = [(n, d) for n, d in self.nodes.items() if (not kinds or d["kind"] in kinds)
                and (not report or report in (d.get("report"), d.get("dataset")))
                and (table is None or d.get("table") == table)]
        exact = [n for n, d in pool if d.get("name") == text]
        found = exact or [n for n, d in pool if text and text in d.get("name", "")]
        return sorted(found, key=lambda n: order.get(self.nodes[n]["kind"], 9))

    def report(self, name: str) -> Report | None:
        """A report by dataset name or report title, ignoring case and spaces."""
        key = re.sub(r"[\s_\-]", "", name).lower()
        return next((r for r in self.reports if key in (re.sub(r"[\s_\-]", "", r.name).lower(),
                                                        re.sub(r"[\s_\-]", "", r.title).lower())), None)

    def neighbours(self, node: str) -> list[tuple[str, str, str]]:
        return [(a, r, b) for a, r, b in self.edges if node in (a, b)]

    def path(self, start: str, goal: str) -> list[tuple[str, str, str]] | None:
        """Cheapest chain of edges between two nodes (edges walked both ways). Meaning-level links (same meaning,
        a measure using another) are preferred over sharing a source column or a table; charts, the author's
        settings and the report node are not walked through (two measures in one chart are not related by that)."""
        adj = defaultdict(list)
        for a, r, b in self.edges:
            w = next((v for k, v in EDGE_COST.items() if r.startswith(k)), 2.0)
            adj[a].append((b, (a, r, b), w))
            adj[b].append((a, (a, r, b), w))
        best, prev, heap = {start: 0.0}, {start: None}, [(0.0, start)]
        while heap:
            cost, n = heapq.heappop(heap)
            if n == goal:
                out = []
                while prev[n]:
                    n, e = prev[n]
                    out.append(e)
                return out[::-1]
            if cost > best.get(n, math.inf) or (n != start and n.split(":", 1)[0] in ("visual", "emphasis", "report")):
                continue
            for m, e, w in adj[n]:
                if cost + w < best.get(m, math.inf):
                    best[m], prev[m] = cost + w, (n, e)
                    heapq.heappush(heap, (cost + w, m))
        return None

    def path_text(self, path: list[tuple[str, str, str]], start: str) -> list[str]:
        """The chain read from start: 'A —relation→ B' when the edge points forward, 'A ←relation— B' otherwise."""
        out, cur = [], start
        for a, r, b in path:
            if a == cur:
                out.append(f"{self.label(a)} —{r}→ {self.label(b)}")
                cur = b
            else:
                out.append(f"{self.label(b)} ←{r}— {self.label(a)}")
                cur = a
        return out

    def label(self, node: str) -> str:
        d = self.nodes.get(node, {})
        kind = {"measure": "量值", "column": "欄位", "table": "資料表", "report": "報表", "source": "來源",
                "source_column": "來源欄位", "visual": "圖表", "emphasis": "作者設定"}.get(d.get("kind"), "")
        where = f"`{d['report']}` " if d.get("report") else ""
        name = f"{d.get('table')}[{d['name']}]" if d.get("kind") == "column" else d.get("name", node)
        return f"{where}{kind}「{name}」"


def build(reports: list[Report], read_data: bool = True) -> Catalog:
    measures, notes = {}, []
    for r in reports:
        for name, c in expand(r).items():
            measures[(r.name, name)] = c
    # measures with the same meaning (union-find over equal pairs)
    items = [c for c in measures.values() if c.tree]
    parent = list(range(len(items)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if root(i) != root(j) and same_meaning(items[i], items[j]):
                parent[root(j)] = root(i)
    groups = defaultdict(list)
    for i, c in enumerate(items):
        groups[root(i)].append(c)
    same = [g for g in groups.values() if len(g) > 1]
    group_key = {(c.report, c.measure): k for k, g in groups.items() for c in g}
    # same name, more than one meaning across reports
    by_name = defaultdict(list)
    for c in measures.values():
        by_name[c.measure].append(c)
    conflicts = []
    for name, cs in by_name.items():
        if len({c.report for c in cs}) < 2:
            continue
        meanings = defaultdict(list)
        for c in cs:
            meanings[group_key.get((c.report, c.measure), ("unexpanded", c.report))].append(c)
        if len(meanings) < 2:
            continue
        gs = list(meanings.values())
        # meanings over the same database / file clash; a meaning over another system only shares the name
        clash = [g for g in gs if any(g is not h and g[0].systems & h[0].systems for h in gs)]
        if clash:
            conflicts.append(Conflict(name, clash, True))
        rest = [g for g in gs if g not in clash]
        if rest:
            conflicts.append(Conflict(name, (clash[:1] if clash else []) + rest, False))
    # physical tables read by more than one report
    shared = defaultdict(list)
    for r in reports:
        for t in r.model.tables.values():
            sid = source_id(t)
            if sid:
                shared[sid].append((r.name, t.name))
    shared_sources = {k: v for k, v in shared.items() if len({x[0] for x in v}) > 1}
    keys = _key_matches(reports, notes) if read_data else []
    cat = Catalog(reports, measures, same, conflicts, shared_sources, keys, notes)
    _graph(cat)
    return cat


def _key_matches(reports: list[Report], notes: list[str]) -> list[KeyMatch]:
    """Value checks on keys:
      - relationships that join two systems (Excel ↔ Oracle): both directions, near misses
      - relationships inside one system: keys on the many side with no row on the one side (orphans)
      - between reports: text columns from different systems that share values (a join nobody modelled yet)"""
    values = Values()
    found: dict[tuple[str, str], KeyMatch] = {}

    def add(kind, ra, ta, ca, rb, tb, cb):
        ka, kb = col_key(ra, ta, ca), col_key(rb, tb, cb)
        pair = (ka, kb)
        if pair in found:  # the same physical pair seen from another report
            found[pair].left.append(f"{ra.name}:{ta}[{ca}]")
            found[pair].right.append(f"{rb.name}:{tb}[{cb}]")
            return found[pair]
        a, b = values.get(ra, ta, ca), values.get(rb, tb, cb)
        if not a or not b:
            return None
        shared, left, right, near = compare(a, b)
        note = ""
        if shared == 0 and kind == "same_file":
            note = _as_dates(a, b)
            if note:
                left, right = [], []
        km = KeyMatch(kind, [f"{ra.name}:{ta}[{ca}]"], [f"{rb.name}:{tb}[{cb}]"], ka, kb, shared, left, right, near,
                      len(a), len(b), note)
        found[pair] = km
        return km

    for r in reports:
        for rel in r.model.relationships:
            ft, tt = r.model.tables.get(rel.from_table), r.model.tables.get(rel.to_table)
            if not ft or not tt or not source_id(ft) or not source_id(tt):
                continue
            kind = "orphans" if system_of(source_id(ft)) == system_of(source_id(tt)) else "relationship"
            add(kind, r, rel.from_table, rel.from_column, r, rel.to_table, rel.to_column)
    # the same workbook / sheet at another path (an uploaded copy of a report's Excel source): every column the
    # two have in common is compared, so "it is the same file" is checked on the data, not taken from the name
    by_file = {}
    for r in reports:
        for t in r.model.tables.values():
            sid = source_id(t)
            if sid and sid.split(":", 1)[0] in ("excel", "csv"):
                path, _, item = sid.split(":", 1)[1].partition("//")
                by_file.setdefault((url_unquote(path).rsplit("/", 1)[-1], item), []).append((r, t, sid))
    for uses in by_file.values():
        for i in range(len(uses)):
            for j in range(i + 1, len(uses)):
                (ra, ta, sa), (rb, tb, sb) = uses[i], uses[j]
                if ra.name == rb.name or system_of(sa) == system_of(sb):
                    continue
                cols_b = {(c.source_column or c.name): c.name for c in tb.columns.values()}
                for c in list(ta.columns.values())[:30]:
                    other = cols_b.get(c.source_column or c.name)
                    if other:
                        add("same_file", ra, ta.name, c.name, rb, tb.name, other)
    # between reports: text columns of different systems
    cand = []
    for r in reports:
        for t in r.model.tables.values():
            for c in t.columns.values():
                key = col_key(r, t.name, c.name)
                if key and c.data_type == "string":
                    cand.append((r, t.name, c.name, key))
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            ra, ta, ca, ka = cand[i]
            rb, tb, cb, kb = cand[j]
            if ra.name == rb.name or system_of(split_key(ka)[0]) == system_of(split_key(kb)[0]) \
                    or (ka, kb) in found or (kb, ka) in found:
                continue
            a, b = values.get(ra, ta, ca), values.get(rb, tb, cb)
            if not a or not b or _numeric_share(a) > 0.5 or _numeric_share(b) > 0.5:
                continue
            shared = len(a & b)
            if shared >= MIN_SHARED and shared / min(len(a), len(b)) >= MIN_CONTAINMENT:
                add("overlap", ra, ta, ca, rb, tb, cb)
    notes += sorted(set(values.notes))
    out = []
    for k in found.values():
        if k.kind == "orphans" and not k.left_only:
            continue  # every key on the many side has its row: nothing to say
        out.append(k)
    return out


# ------------------------------------------------------------------------------------------- graph
def _graph(cat: Catalog) -> None:
    N, E = cat.nodes, []

    def node(nid, kind, name, **extra):
        N.setdefault(nid, {"kind": kind, "name": name, **extra})
        return nid

    for r in cat.reports:
        rid = node(f"report:{r.name}", "report", r.title, dataset=r.name)
        for t in r.model.tables.values():
            tid = node(f"table:{r.name}:{t.name}", "table", t.name, report=r.name)
            E.append((rid, "包含資料表", tid))
            sid = source_id(t)
            if sid:
                E.append((tid, "讀取", node(f"source:{sid}", "source", source_label(sid))))
            for c in t.columns.values():
                cid = node(f"column:{r.name}:{t.name}[{c.name}]", "column", c.name, report=r.name, table=t.name)
                E.append((tid, "欄位", cid))
                key = column_source(t, c.name)
                if key:
                    E.append((cid, "來自", node(f"srccol:{key}", "source_column", short(key))))
        for rel in r.model.relationships:
            E.append((f"column:{r.name}:{rel.from_table}[{rel.from_column}]", "模型關聯",
                      f"column:{r.name}:{rel.to_table}[{rel.to_column}]"))
        for name, m in r.model.measures.items():
            mid = node(f"measure:{r.name}:{name}", "measure", name, report=r.name, table=m.table,
                       expression=m.expression, description=m.description)
            E.append((f"table:{r.name}:{m.table}", "量值", mid))
            for ref in dict.fromkeys(re.findall(r"(?<![\w'\]])\[([^\]]+)\]", m.expression)):
                if ref in r.model.measures and ref != name:
                    E.append((mid, "引用", f"measure:{r.name}:{ref}"))
        for u in r.model.usage:
            vid = node(f"visual:{r.name}:{u.page}/{u.visual}", "visual", u.title or u.visual_type, report=r.name,
                       page=u.page)
            E.append((rid, "頁面圖表", vid))
            for m in u.measures:
                E.append((vid, "顯示", f"measure:{r.name}:{m}"))
            for c in u.columns:
                t, _, col = c.partition(".")
                E.append((vid, "顯示", f"column:{r.name}:{t}[{col}]"))
        for i, e in enumerate(r.emphasis):
            if e.kind in ("layout", "bookmarks", "hidden_page"):
                continue
            eid = node(f"emphasis:{r.name}:{i}", "emphasis", e.text, report=r.name, page=e.page,
                       emphasis=e.kind, thresholds=e.thresholds)
            target = _emphasis_target(r, e.field)
            E.append((eid, "凸顯", target) if target else (f"report:{r.name}", "作者設定", eid))
    for c in cat.measures.values():
        for key in sorted({k for a in c.atoms for k in KEY.findall(a)}):
            nid = f"srccol:{key}"
            if nid in N:
                E.append((f"measure:{c.report}:{c.measure}", "計算自", nid))
    for g in cat.same:
        for c in g[1:]:
            E.append((f"measure:{g[0].report}:{g[0].measure}", "同義（公式等價）", f"measure:{c.report}:{c.measure}"))
    for cf in cat.conflicts:
        for i, a in enumerate(cf.groups):
            for b in cf.groups[i + 1:]:
                E.append((f"measure:{a[0].report}:{a[0].measure}",
                          "同名但定義不同" if cf.same_system else "同名（不同系統，非同一件事）",
                          f"measure:{b[0].report}:{b[0].measure}"))
    for k in cat.keys:
        rel = {"relationship": "跨系統代碼對應", "orphans": "關聯有孤兒值", "overlap": "跨報表值重疊",
               "same_file": "同一份檔案的同一欄"}[k.kind]
        E.append(("column:" + k.left[0], f"{rel}（相同 {k.shared} 個）", "column:" + k.right[0]))
    cat.edges[:] = list(dict.fromkeys(E))


def _emphasis_target(r: Report, field_: str) -> str | None:
    """Node of the measure / column an emphasis is about ('毛利率', '門市[區域]', 'Sum(門市.區域)')."""
    if not field_:
        return None
    m = re.fullmatch(r"(?:\w+\()?([^\[\(]+)\[([^\]]+)\]\)?", field_)
    if m and m.group(1) in r.model.tables:
        return f"column:{r.name}:{m.group(1)}[{m.group(2)}]"
    name = re.sub(r"^\w+\((.*)\)$", r"\1", field_)
    if name in r.model.measures:
        return f"measure:{r.name}:{name}"
    return None


# ------------------------------------------------------------------------------------------- output
def _names(cs: list[Canonical]) -> str:
    return "、".join(f"`{c.report}`「{c.measure}」" for c in cs)


def to_markdown(cat: Catalog) -> str:
    L = ["# 報表目錄：多份報表之間的關係", "",
         "> 由 `python -m analysis catalog` 產生。資料來源、量值定義與關聯由報表檔案（TMDL、Power Query、版面）讀出；"
         "代碼對應讀實際資料逐值比對。量值「同義」= 展開成實體欄位上的彙總後公式等價，與名稱、寫法無關。", "",
         "## 報表", "", "| 報表 | 資料集 | 資料表 | 量值 | 讀取的來源 |", "|---|---|---|---|---|"]
    for r in cat.reports:
        systems = sorted({source_label(s).rsplit(".", 1)[0] if s.startswith("oracle:") else source_label(s).split("／")[0]
                          for s in (source_id(t) for t in r.model.tables.values()) if s})
        L.append(f"| {r.title} | `{r.name}` | {len(r.model.tables)} | {len(r.model.measures)} | {'、'.join(systems)} |")
    L += ["", "## 共用的資料來源", "", "同一張實體資料表被多份報表讀取（不論在報表裡叫什麼名字）。", ""]
    if cat.shared_sources:
        L += ["| 來源 | 各報表裡的名稱 |", "|---|---|"]
        for sid, uses in sorted(cat.shared_sources.items(), key=lambda x: source_label(x[0])):
            L.append(f"| {source_label(sid)} | {'、'.join(f'`{r}`：{t}' for r, t in uses)} |")
    else:
        L.append("（沒有）")
    L += ["", "## 同義量值（公式等價）", ""]
    multi = [g for g in cat.same if len({c.report for c in g}) > 1]
    renamed = [g for g in multi if len({c.measure for c in g}) > 1]
    if renamed:
        L += ["**名稱不同、意思相同**（最容易被忽略）：", ""]
        L += [f"- {_names(g)}：{g[0].readable()}" for g in renamed]
        L.append("")
    plain = [g for g in multi if len({c.measure for c in g}) == 1]
    if plain:
        L.append("名稱與意思都相同：" + "；".join(f"「{g[0].measure}」（{'、'.join(f'`{c.report}`' for c in g)}）"
                                                for g in plain))
        L.append("")
    inner = [g for g in cat.same if len({c.report for c in g}) == 1]
    if inner:
        L += ["同一份報表內公式等價的量值：", ""]
        L += [f"- `{g[0].report}`：{'、'.join('「' + c.measure + '」' for c in g)}：{g[0].readable()}" for g in inner]
        L.append("")
    if not cat.same:
        L.append("（沒有）")
    L += ["## 同名但定義不同", ""]
    real = [c for c in cat.conflicts if c.same_system]
    if real:
        L += ["讀同一個資料庫 / 檔案，名稱相同但算法不同：兩份報表的數字會對不起來。", ""]
        for cf in real:
            L.append(f"### 「{cf.name}」")
            L.append("")
            for g in cf.groups:
                L.append(f"- {_names(g)}：{g[0].readable()}")
            L += [f"- 差別：{_difference(cf.groups[0][0], g[0])}" for g in cf.groups[1:]]
            L.append("")
    other = [c for c in cat.conflicts if not c.same_system]
    if other:
        L += ["讀不同系統的同名量值（不同的業務資料，名稱相同不代表同一件事）：" +
              "、".join(f"「{cf.name}」（{'／'.join(_names(g) for g in cf.groups)}）" for cf in other), ""]
    if not cat.conflicts:
        L += ["（沒有）", ""]
    L += ["## 代碼對應（讀實際資料比對）", ""]
    titles = {"same_file": "同名檔案的同一張工作表、同名欄位（可能是同一份檔案的副本）",
              "relationship": "模型關聯跨兩個系統", "orphans": "關聯的多方有對不到的值（孤兒）",
              "overlap": "不同報表、不同系統的欄位值重疊（可能可以串接）"}
    if cat.keys:
        for k in cat.keys:
            L.append(f"### {'、'.join(k.left)} ↔ {'、'.join(k.right)}")
            L.append("")
            L.append(f"- 類型：{titles[k.kind]}")
            L.append(f"- 來源：{short(k.left_source)}（{k.left_count} 個值） ↔ {short(k.right_source)}"
                     f"（{k.right_count} 個值），相同 {k.shared} 個")
            near_l, near_r = {a for a, _ in k.near}, {b for _, b in k.near}
            if k.note:
                L.append(f"- {k.note}")
            if k.near:
                L.append(f"- 只差大小寫或空白：{'、'.join(f'「{a}」≈「{b}」' for a, b in k.near)}")
            lo = [v for v in k.left_only if v not in near_l]
            ro = [v for v in k.right_only if v not in near_r]
            if lo:
                L.append(f"- 只在 {short(k.left_source)}：{_list(lo)}")
            if ro and k.kind != "orphans":
                L.append(f"- 只在 {short(k.right_source)}：{_list(ro)}")
            if not (lo or ro or k.near or k.note):
                L.append("- 完全一致")
            L.append("")
    else:
        L += ["（沒有，或未讀取資料）", ""]
    bad = [c for c in cat.measures.values() if not c.tree]
    L += ["## 無法展開的量值", "", "用到尚未支援的 DAX，無法判斷與其他報表的量值是否同義（不會被當成相同）。", ""]
    L += [f"- `{c.report}`「{c.measure}」：{c.reason}" for c in bad] or ["（沒有）"]
    L += ["", "## 報表作者的凸顯（摘要）", ""]
    for r in cat.reports:
        explicit = [e for e in r.emphasis if e.explicit and e.kind not in ("bookmarks", "hidden_page")]
        if explicit:
            L.append(f"**{r.title}**（`{r.name}`）")
            L.append("")
            L += [f"- {e.page}／{e.visual}：{e.text}" for e in explicit[:12]]
            if len(explicit) > 12:
                L.append(f"- …另有 {len(explicit) - 12} 項（`python -m analysis intent {r.name}`）")
            L.append("")
    if cat.notes:
        L += ["## 備註", ""] + [f"- {n}" for n in cat.notes]
    return "\n".join(L).rstrip() + "\n"


def _list(values: list[str], n: int = 12) -> str:
    return "、".join(f"「{v}」" for v in values[:n]) + (f"…（共 {len(values)} 個）" if len(values) > n else "")


def _difference(a: Canonical, b: Canonical) -> str:
    if not (a.tree and b.tree):
        return "至少一邊無法展開，無法比對"
    only_a, only_b = a.atoms - b.atoms, b.atoms - a.atoms
    parts = []
    if only_a:
        parts.append(f"`{a.report}` 用到 " + "、".join(_render(("atom", x)) for x in sorted(only_a)))
    if only_b:
        parts.append(f"`{b.report}` 用到 " + "、".join(_render(("atom", x)) for x in sorted(only_b)))
    return ("；".join(parts) + "（另一邊沒有）") if parts else "用到相同的彙總，但組合方式不同"
