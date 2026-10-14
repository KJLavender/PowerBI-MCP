"""Read the Power BI project (TMDL model + PBIR report) and turn it into analysis metadata.

Everything the engine knows about tables, joins and metric formulas comes from here,
so the analysis follows the same relationships and measure definitions as the report.
"""
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .dataset import PBIP_DIR, ROOT  # noqa: F401  (ROOT re-exported)


# ----------------------------------------------------------------------------------------
# Model objects
# ----------------------------------------------------------------------------------------
@dataclass
class Column:
    name: str
    data_type: str
    source_column: str
    description: str = ""
    hidden: bool = False


@dataclass
class Measure:
    table: str
    name: str
    expression: str
    description: str = ""
    format_string: str = ""
    folder: str = ""


@dataclass
class Table:
    name: str
    description: str = ""
    source_table: str | None = None  # physical table / Excel sheet, taken from the M partition
    source: dict = field(default_factory=dict)  # {"kind": "oracle"} or {"kind": "excel", "file": ...}
    columns: dict[str, Column] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)


@dataclass
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    active: bool = True
    from_many: bool = True
    to_one: bool = True

    def __str__(self) -> str:
        return f"{self.from_table}[{self.from_column}] → {self.to_table}[{self.to_column}]"


@dataclass
class VisualUsage:
    page: str
    visual: str
    visual_type: str
    title: str
    measures: list[str]
    columns: list[str]  # "TABLE.COLUMN"


@dataclass
class SemanticModel:
    tables: dict[str, Table]
    relationships: list[Relationship]
    usage: list[VisualUsage]
    expressions: dict[str, str] = field(default_factory=dict)  # Power Query parameters (name → text value)

    @property
    def measures(self) -> dict[str, Measure]:
        return {m.name: m for t in self.tables.values() for m in t.measures.values()}

    def join_path(self, fact: str, target: str) -> list[Relationship]:
        """Shortest chain of active many-to-one relationships from the fact table to target."""
        if fact == target:
            return []
        queue, seen = deque([(fact, [])]), {fact}
        while queue:
            table, path = queue.popleft()
            for r in self.relationships:
                if r.active and r.from_many and r.to_one and r.from_table == table and r.to_table not in seen:
                    if r.to_table == target:
                        return path + [r]
                    seen.add(r.to_table)
                    queue.append((r.to_table, path + [r]))
        raise ValueError(f"no many-to-one relationship path from {fact} to {target}")

    def used_measures(self) -> list[str]:
        """Measures shown in the report, in page order (what users actually look at)."""
        out = []
        for u in self.usage:
            for m in u.measures:
                if m not in out:
                    out.append(m)
        return out


# ----------------------------------------------------------------------------------------
# TMDL parsing (the subset Power BI Desktop writes for import models)
# ----------------------------------------------------------------------------------------
NAME = r"(?:'(?:[^']|'')*'|[^\s=']+)"


def unquote(name: str) -> str:
    name = name.strip()
    return name[1:-1].replace("''", "'") if name.startswith("'") else name


def split_ref(ref: str) -> tuple[str, str]:
    """'Table Name'.Column  ->  (Table Name, Column)"""
    m = re.match(rf"({NAME})\.({NAME})$", ref.strip())
    if not m:
        raise ValueError(f"bad column reference {ref!r}")
    return unquote(m.group(1)), unquote(m.group(2))


def depth(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def parse_table(text: str) -> Table:
    lines = text.splitlines()
    table: Table | None = None
    doc: list[str] = []
    obj = None  # current Column / Measure / "partition"
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        d = depth(line)
        if s.startswith("///"):
            doc.append(s[3:].strip())
        elif d == 0 and s.startswith("table "):
            table = Table(unquote(s[6:]), " ".join(doc))
            doc = []
        elif d == 1 and s.startswith("measure "):
            m = re.match(rf"measure ({NAME})\s*=\s*(.*)$", s)
            expr = m.group(2)
            while i + 1 < len(lines) and depth(lines[i + 1]) >= 3 and lines[i + 1].strip():
                i += 1
                expr += "\n" + lines[i].strip()
            obj = Measure(table.name, unquote(m.group(1)), expr.strip().strip("`").strip(), " ".join(doc))
            table.measures[obj.name] = obj
            doc = []
        elif d == 1 and s.startswith("column "):
            obj = Column(unquote(s[7:]), "", "", " ".join(doc))
            table.columns[obj.name] = obj
            doc = []
        elif d == 1 and s.startswith("partition "):
            obj = "partition"
            doc = []
        elif d == 1:
            obj, doc = None, []
        elif d >= 2 and obj is not None:
            if isinstance(obj, Column):
                if s.startswith("dataType:"):
                    obj.data_type = s.split(":", 1)[1].strip()
                elif s.startswith("sourceColumn:"):
                    obj.source_column = s.split(":", 1)[1].strip()
                elif s == "isHidden":
                    obj.hidden = True
            elif isinstance(obj, Measure):
                if s.startswith("formatString:"):
                    obj.format_string = s.split(":", 1)[1].strip()
                elif s.startswith("displayFolder:"):
                    obj.folder = s.split(":", 1)[1].strip()
            elif obj == "partition":
                # Oracle navigation {[Name = "TABLE"]} or Excel sheet {[Item = "Sheet", Kind = "Sheet"]}
                m = re.search(r'\{\[\s*(?:Name|Item)\s*=\s*"([^"]+)"', s)
                if m and table.source_table is None:
                    table.source_table = m.group(1)
                if "Oracle.Database" in s:
                    table.source = {"kind": "oracle"}
                m = re.search(r'Excel\.Workbook\s*\(\s*File\.Contents\s*\(\s*("(?:[^"]|"")*"|[^)\s]+)', s)
                if m:
                    table.source = {"kind": "excel", "file": m.group(1)}
        i += 1
    return table


def parse_relationships(text: str) -> list[Relationship]:
    rels, cur = [], None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("relationship "):
            cur = {}
            rels.append(cur)
        elif cur is not None and ":" in s:
            k, v = (x.strip() for x in s.split(":", 1))
            cur[k] = v
    out = []
    for r in rels:
        ft, fc = split_ref(r["fromColumn"])
        tt, tc = split_ref(r["toColumn"])
        out.append(Relationship(
            ft, fc, tt, tc,
            active=r.get("isActive", "true") != "false",
            from_many=r.get("fromCardinality", "many") == "many",
            to_one=r.get("toCardinality", "one") == "one",
        ))
    return out


def parse_report(report_dir: Path) -> list[VisualUsage]:
    pages_dir = report_dir / "definition" / "pages"
    order = json.loads((pages_dir / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    usage = []
    for page in order:
        page_json = json.loads((pages_dir / page / "page.json").read_text(encoding="utf-8"))
        for vf in sorted((pages_dir / page / "visuals").glob("*/visual.json")):
            v = json.loads(vf.read_text(encoding="utf-8"))
            visual = v.get("visual", {})
            measures, columns = [], []
            for f in _fields(visual.get("query", {})):
                kind, body = next(iter(f.items()))
                entity = body["Expression"]["SourceRef"]["Entity"]
                (measures if kind == "Measure" else columns).append(
                    body["Property"] if kind == "Measure" else f"{entity}.{body['Property']}")
            title = ""
            for t in visual.get("visualContainerObjects", {}).get("title", []):
                lit = t.get("properties", {}).get("text", {}).get("expr", {}).get("Literal", {})
                title = lit.get("Value", "").strip("'")
            usage.append(VisualUsage(page_json.get("displayName", page), v["name"],
                                     visual.get("visualType", ""), title, measures, columns))
    return usage


def _fields(node):
    if isinstance(node, dict):
        if "field" in node and isinstance(node["field"], dict):
            yield node["field"]
        for value in node.values():
            yield from _fields(value)
    elif isinstance(node, list):
        for value in node:
            yield from _fields(value)


def parse_expressions(text: str) -> dict[str, str]:
    """Text parameters, e.g. expression ExcelPath = "D:/data/book.xlsx" meta [IsParameterQuery=true, ...]"""
    out = {}
    for m in re.finditer(rf'^expression ({NAME})\s*=\s*"((?:[^"]|"")*)"', text, re.M):
        out[unquote(m.group(1))] = m.group(2).replace('""', '"')
    return out


def load(pbip_dir: Path = PBIP_DIR) -> SemanticModel:
    model_dir = next(pbip_dir.glob("*.SemanticModel")) / "definition"
    report_dir = next(pbip_dir.glob("*.Report"))
    tables = {}
    for f in sorted((model_dir / "tables").glob("*.tmdl")):
        t = parse_table(f.read_text(encoding="utf-8"))
        tables[t.name] = t
    rels = parse_relationships((model_dir / "relationships.tmdl").read_text(encoding="utf-8"))
    expr_file = model_dir / "expressions.tmdl"
    expressions = parse_expressions(expr_file.read_text(encoding="utf-8")) if expr_file.exists() else {}
    for t in tables.values():
        ref = t.source.get("file")
        if ref:  # a literal path or the name of a text parameter
            t.source["file"] = ref[1:-1].replace('""', '"') if ref.startswith('"') else expressions.get(ref, ref)
    return SemanticModel(tables, rels, parse_report(report_dir), expressions)


def ident(name: str) -> str:
    """SQL identifier: plain names stay as they are (Oracle upper-case columns), others are quoted."""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", name) else '"' + name.replace('"', '""') + '"'


def dax_table(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def dax_col(table: str, column: str) -> str:
    return f"{dax_table(table)}[{column.replace(']', ']]')}]"


def safe_name(name: str) -> str:
    """Column name usable as a Python identifier in formula expressions ("Order ID" → Order_ID)."""
    return re.sub(r"\W+", "_", name).strip("_")


# ----------------------------------------------------------------------------------------
# DAX → formula translation (additive building blocks + arithmetic)
# ----------------------------------------------------------------------------------------
class Unsupported(Exception):
    pass


@dataclass
class Base:
    """An aggregate over the fact table, in SQL (for the source database) and DAX (for the Power BI engine)."""
    name: str
    sql: str            # aggregate over fact alias f, e.g. SUM(f.SALES_AMOUNT)
    distinct_of: str | None = None  # column for COUNT(DISTINCT ...), not additive across all grains
    dax: str = ""       # the same aggregate in DAX, e.g. SUM('FACT_SALES'[SALES_AMOUNT])


@dataclass
class Formula:
    measure: str
    expr: str           # python expression over base names, using div(a, b)
    bases: list[str]
    refs: list[str]     # measures referenced directly
    is_ratio: bool
    numerator: str | None = None    # for ratios: expr of numerator / denominator
    denominator: str | None = None


TOKEN = re.compile(r"""
    \s*(?:
      (?P<num>\d+(?:\.\d+)?)
    | (?P<colref>(?:'(?:[^']|'')*'|[A-Za-z_][\w]*)\s*\[[^\]]+\])
    | (?P<mref>\[[^\]]+\])
    | (?P<func>[A-Za-z_][\w.]*)(?=\s*\()
    | (?P<ident>'(?:[^']|'')*'|[A-Za-z_]\w*)
    | (?P<op>[-+*/(),])
    )""", re.X)


def tokenize(expr: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise Unsupported(f"cannot parse DAX near {expr[pos:pos + 20]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind).strip()))
        pos = m.end()
    return out


class Translator:
    def __init__(self, model: SemanticModel, fact: str):
        self.model = model
        self.fact = fact
        self.bases: dict[str, Base] = {}
        self.formulas: dict[str, Formula] = {}

    def _col(self, ref: str) -> tuple[str, str]:
        """(DAX reference, source column) of a fact column."""
        m = re.match(r"('(?:[^']|'')*'|[A-Za-z_]\w*)\s*\[([^\]]+)\]", ref)
        table, col = unquote(m.group(1)), m.group(2)
        if table != self.fact:
            raise Unsupported(f"column {table}[{col}] is not on the fact table")
        return dax_col(table, col), self.model.tables[table].columns[col].source_column or col

    def _base(self, name: str, sql: str, distinct_of: str | None = None, dax: str = "") -> str:
        self.bases.setdefault(name, Base(name, sql, distinct_of, dax))
        return name

    def translate(self, measure: str) -> Formula:
        if measure in self.formulas:
            return self.formulas[measure]
        m = self.model.measures[measure]
        self.tokens, self.i, self.refs, self.used = tokenize(m.expression), 0, [], []
        expr = self._expr()
        if self.i != len(self.tokens):
            raise Unsupported("trailing tokens")
        top = getattr(self, "_top_div", None)
        f = Formula(measure, expr, sorted(set(self.used)), self.refs, "div(" in expr)
        if expr.startswith("div(") and top:
            f.numerator, f.denominator = top
        self._top_div = None
        self.formulas[measure] = f
        return f

    # recursive descent: expr := term (+|- term)* ; term := factor (*|/ factor)*
    def _peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def _take(self, kind=None, value=None):
        tok = self._peek()
        if (kind and tok[0] != kind) or (value and tok[1] != value):
            raise Unsupported(f"expected {value or kind}, got {tok}")
        self.i += 1
        return tok

    def _expr(self) -> str:
        out = self._term()
        while self._peek() in (("op", "+"), ("op", "-")):
            out += f" {self._take()[1]} {self._term()}"
        return out

    def _term(self) -> str:
        out = self._factor()
        while self._peek() in (("op", "*"), ("op", "/")):
            op = self._take()[1]
            rhs = self._factor()
            out = f"div({out}, {rhs})" if op == "/" else f"{out} * {rhs}"
        return out

    def _factor(self) -> str:
        kind, val = self._peek()
        if kind == "num":
            self.i += 1
            return val
        if (kind, val) == ("op", "("):
            self.i += 1
            inner = self._expr()
            self._take("op", ")")
            return f"({inner})"
        if kind == "mref":
            self.i += 1
            name = val[1:-1]
            if name not in self.model.measures:
                raise Unsupported(f"unknown measure {name}")
            saved = (self.tokens, self.i, self.refs, self.used)
            sub = self.translate(name)
            self.tokens, self.i, self.refs, self.used = saved
            self.refs.append(name)
            self.used += sub.bases
            return f"({sub.expr})"
        if kind == "func":
            return self._func(val.upper())
        raise Unsupported(f"unsupported token {val!r}")

    def _func(self, fn: str) -> str:
        self.i += 1
        self._take("op", "(")
        if fn in ("SUM", "DISTINCTCOUNT"):
            ref, col = self._col(self._take("colref")[1])
            self._take("op", ")")
            if fn == "SUM":
                name = self._base(f"sum_{safe_name(col)}", f"SUM(f.{ident(col)})", dax=f"SUM({ref})")
            else:
                name = self._base(f"dc_{safe_name(col)}", f"COUNT(DISTINCT f.{ident(col)})", distinct_of=col,
                                  dax=f"DISTINCTCOUNT({ref})")
            self.used.append(name)
            return name
        if fn == "COUNTROWS":
            table = unquote(self._take()[1])
            self._take("op", ")")
            if table != self.fact:
                raise Unsupported("COUNTROWS on non-fact table")
            self.used.append(self._base("rows", "COUNT(*)", dax=f"COUNTROWS({dax_table(table)})"))
            return "rows"
        if fn == "SUMX":
            table = unquote(self._take()[1])
            self._take("op", ",")
            if table != self.fact:
                raise Unsupported("SUMX over non-fact table")
            sql, dax = self._row_expr()
            self._take("op", ")")
            key = "sumx_" + safe_name(sql.replace("f.", "").replace('"', ""))
            self.used.append(self._base(key, f"SUM({sql})", dax=f"SUMX({dax_table(table)}, {dax})"))
            return key
        if fn == "DIVIDE":
            num = self._expr()
            self._take("op", ",")
            den = self._expr()
            self._take("op", ")")
            if self.i == len(self.tokens):  # DIVIDE is the whole measure: remember its parts
                self._top_div = (num, den)
            return f"div({num}, {den})"
        raise Unsupported(f"function {fn} not supported")

    def _row_expr(self) -> tuple[str, str]:
        """Row-level arithmetic over fact columns inside SUMX, rendered as SQL and as DAX."""
        parts, dax = [], []
        while self._peek()[0] in ("colref", "op", "num") and self._peek() != ("op", ",") \
                and not (self._peek() == ("op", ")") and parts.count("(") <= parts.count(")")):
            kind, val = self._take()
            if kind == "colref":
                ref, col = self._col(val)
                parts.append(f"f.{ident(col)}")
                dax.append(ref)
            else:
                parts.append(val)
                dax.append(val)
        if not parts:
            raise Unsupported("empty SUMX expression")
        return " ".join(parts), " ".join(dax)


def translate_all(model: SemanticModel, fact: str) -> tuple[dict[str, Formula], dict[str, Base], dict[str, str]]:
    """Translate every measure on the fact table; returns formulas, bases and skip reasons."""
    tr = Translator(model, fact)
    skipped = {}
    for name, m in model.measures.items():
        if m.table != fact:
            continue
        try:
            tr.translate(name)
        except (Unsupported, KeyError) as e:
            skipped[name] = str(e)
            tr.formulas.pop(name, None)
    return tr.formulas, tr.bases, skipped
