"""Read the Power BI project (TMDL model + PBIR report) and turn it into analysis metadata.

Everything the engine knows about tables, joins and metric formulas comes from here,
so the analysis follows the same relationships and measure definitions as the report.
"""
import json
import re
from collections import deque
from datetime import date
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
    calculated: bool = False  # no source column: a DAX calculated column (its values exist only in a .pbix)


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
    source: dict = field(default_factory=dict)  # {"kind": "oracle"} / {"kind": "excel" | "csv", "file": ...}
    m: str = ""                                 # the Power Query (M) of the partition
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
    filters: list[str] = field(default_factory=list)  # page- and visual-level filters, readable text


@dataclass
class SemanticModel:
    tables: dict[str, Table]
    relationships: list[Relationship]
    usage: list[VisualUsage]
    expressions: dict[str, str] = field(default_factory=dict)  # Power Query parameters (name → text value)
    report_filters: list[tuple] = field(default_factory=list)  # report-level filters: (table, column, op, values)
    filter_notes: list[str] = field(default_factory=list)      # report-level filters that cannot be applied

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
                table.m += s + "\n"
        i += 1
    if table and table.m:
        table.source, table.source_table = m_source(table.m)
    for c in (table.columns.values() if table else []):
        if not c.source_column:  # stored in the .pbix under its own name
            c.source_column, c.calculated = c.name, True
    return table


# Power Query steps that only reshape columns: reading the file directly still gives the same rows
HARMLESS_STEPS = {"Excel.Workbook", "File.Contents", "Csv.Document", "Oracle.Database", "Table.PromoteHeaders",
                  "Table.TransformColumnTypes", "Table.RenameColumns", "Table.AddColumn", "Table.RemoveColumns",
                  "Table.ReorderColumns", "Table.SelectColumns"}
CODEPAGES = {"65001": "utf-8", "950": "cp950", "936": "gbk", "1252": "cp1252", "932": "cp932", "1200": "utf-16"}


def m_source(m: str) -> tuple[dict, str | None]:
    """Where a partition reads from, as far as it can be replicated without Power BI.

    Oracle tables, Excel sheets / Excel tables, CSV files. `steps` lists the Table.* functions that change the
    rows (filters, merges, unpivots …): with those, the file alone does not give the report's data."""
    nav = re.search(r'\{\[\s*(?:Name|Item)\s*=\s*"([^"]+)"(?:\s*,\s*Kind\s*=\s*"(\w+)")?', m)
    functions = set(re.findall(r"\b([A-Z][A-Za-z]*\.[A-Z][A-Za-z]*)\s*\(", m))
    steps = sorted(f for f in functions if f.startswith("Table.") and f not in HARMLESS_STEPS)
    renames = re.findall(r'\{\s*"((?:[^"]|"")*)"\s*,\s*"((?:[^"]|"")*)"\s*\}',
                         m[m.find("Table.RenameColumns"):] if "Table.RenameColumns" in m else "")
    common = {"steps": steps, "promote": "Table.PromoteHeaders" in m, "renames": renames}
    if "Oracle.Database" in m:
        # server and schema: a "literal" or the name of a text parameter (resolved in load)
        server = re.search(r'Oracle\.Database\s*\(\s*("(?:[^"]|"")*"|[^,)\s]+)', m)
        schema = re.search(r'\[\s*Schema\s*=\s*("(?:[^"]|"")*"|[^\]\s]+)\s*\]', m)
        return {"kind": "oracle", **({"server": server.group(1)} if server else {}),
                **({"schema": schema.group(1)} if schema else {})}, nav.group(1) if nav else None
    file_ref = r'File\.Contents\s*\(\s*("(?:[^"]|"")*"|[^)\s]+)'
    if x := re.search(r"Excel\.Workbook\s*\(\s*" + file_ref, m):
        return {"kind": "excel", "file": x.group(1), "item_kind": (nav.group(2) if nav and nav.group(2) else "Sheet"),
                **common}, nav.group(1) if nav else None
    if x := re.search(r"Csv\.Document\s*\(\s*" + file_ref, m):
        delim = re.search(r'Delimiter\s*=\s*"((?:[^"]|"")*)"', m)
        enc = re.search(r"Encoding\s*=\s*(\d+)", m)
        return {"kind": "csv", "file": x.group(1), "delimiter": delim.group(1) if delim else ",",
                "encoding": CODEPAGES.get(enc.group(1), "utf-8") if enc else "utf-8", **common}, None
    return {}, nav.group(1) if nav else None


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


def parse_report(report_dir: Path) -> tuple[list[VisualUsage], list[tuple], list[str]]:
    """Visual usage (with page/visual filters as text) and the report-level filters."""
    pages_dir = report_dir / "definition" / "pages"
    order = json.loads((pages_dir / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    report_json = report_dir / "definition" / "report.json"
    report_filters, notes = parse_filters(
        json.loads(report_json.read_text(encoding="utf-8")).get("filterConfig", {}).get("filters", [])
        if report_json.exists() else [])
    usage = []
    for page in order:
        page_json = json.loads((pages_dir / page / "page.json").read_text(encoding="utf-8"))
        page_conds, page_bad = parse_filters(page_json.get("filterConfig", {}).get("filters", []))
        page_text = [f"頁面篩選：{describe_filter(c)}" for c in page_conds] + [f"頁面篩選（無法解讀）：{x}" for x in page_bad]
        for vf in sorted((pages_dir / page / "visuals").glob("*/visual.json")):
            v = json.loads(vf.read_text(encoding="utf-8"))
            visual = v.get("visual", {})
            measures, columns = [], []
            for f in _fields(visual.get("query", {})):
                kind, body = next(iter(f.items()))
                if kind == "Aggregation":  # implicit measure, e.g. Sum(Table.Column): the column is used
                    kind, body = next(iter(body.get("Expression", {}).items()), (None, {}))
                ref = body.get("Expression", {}).get("SourceRef", {}) if isinstance(body, dict) else {}
                if kind not in ("Measure", "Column") or "Entity" not in ref:
                    continue  # hierarchy levels, visual calculations …
                (measures if kind == "Measure" else columns).append(
                    body["Property"] if kind == "Measure" else f"{ref['Entity']}.{body['Property']}")
            title = ""
            for t in visual.get("visualContainerObjects", {}).get("title", []):
                lit = t.get("properties", {}).get("text", {}).get("expr", {}).get("Literal", {})
                title = lit.get("Value", "").strip("'")
            v_conds, v_bad = parse_filters(v.get("filterConfig", {}).get("filters", []))
            text = page_text + [f"此圖篩選：{describe_filter(c)}" for c in v_conds] + \
                [f"此圖篩選（無法解讀）：{x}" for x in v_bad]
            usage.append(VisualUsage(page_json.get("displayName", page), v["name"],
                                     visual.get("visualType", ""), title, measures, columns, text))
    return usage, report_filters, notes


# ---- report filters (PBIR filterConfig / legacy Layout "filters": the same query-filter JSON)
COMPARISON = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}
NEGATE = {"=": "<>", "<>": "=", "IN": "NOT IN", "NOT IN": "IN", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}


def parse_filters(entries: list) -> tuple[list[tuple], list[str]]:
    """Filters with a selection → conditions (table, column, op, values); anything else → readable note."""
    conds, bad = [], []
    for f in entries or []:
        flt = f.get("filter") if isinstance(f, dict) else None
        if not flt:
            continue  # a field in the filter pane with nothing selected
        alias = {x.get("Name"): x.get("Entity") for x in flt.get("From", [])}
        try:
            for w in flt.get("Where", []):
                conds += _condition(w["Condition"], alias)
        except (Unsupported, KeyError, TypeError, ValueError) as e:
            field_ = f.get("field", {})
            name = next(iter(field_.values()), {}).get("Property", "?") if field_ else "?"
            bad.append(f"{name}（{f.get('type', '')}：{e}）")
    return conds, bad


def _column_of(node: dict, alias: dict) -> tuple[str, str]:
    col = node["Column"]
    ref = col["Expression"]["SourceRef"]
    return ref.get("Entity") or alias[ref["Source"]], col["Property"]


def _literal_value(lit: dict):
    v = lit["Literal"]["Value"]
    if v.startswith("datetime'"):
        return date.fromisoformat(v[9:19])
    if v.startswith("'"):
        return v[1:-1].replace("''", "'")
    if v[-1:] in ("L", "D", "M") and re.fullmatch(r"-?\d+(\.\d+)?[LDM]", v):
        return int(v[:-1]) if v.endswith("L") else float(v[:-1])
    if v == "null":
        raise Unsupported("篩選空白值")
    raise Unsupported(f"值 {v}")


def _condition(node: dict, alias: dict) -> list[tuple]:
    if "And" in node:
        return _condition(node["And"]["Left"], alias) + _condition(node["And"]["Right"], alias)
    if "Not" in node:
        inner = _condition(node["Not"]["Expression"], alias)
        if len(inner) != 1:
            raise Unsupported("NOT 多個條件")
        t, c, op, values = inner[0]
        return [(t, c, NEGATE[op], values)]
    if "In" in node:
        exprs = node["In"]["Expressions"]
        if len(exprs) != 1 or "Values" not in node["In"]:
            raise Unsupported("多欄或子查詢篩選（例如前 N 名）")
        t, c = _column_of(exprs[0], alias)
        return [(t, c, "IN", tuple(_literal_value(v[0]) for v in node["In"]["Values"]))]
    if "Comparison" in node:
        cmp = node["Comparison"]
        t, c = _column_of(cmp["Left"], alias)
        return [(t, c, COMPARISON[cmp["ComparisonKind"]], (_literal_value(cmp["Right"]),))]
    raise Unsupported(f"條件類型 {next(iter(node), '?')}（例如 OR、相對日期、前 N 名）")


def describe_filter(c: tuple) -> str:
    t, col, op, values = c
    text = {"IN": "只含", "NOT IN": "排除", "=": "=", "<>": "≠", ">": ">", ">=": "≥", "<": "<", "<=": "≤"}[op]
    return f"{t}[{col}] {text} {'、'.join(str(v) for v in values)}"


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
        for key in ("file", "server", "schema"):
            ref = t.source.get(key)
            if ref:  # a literal or the name of a text parameter
                t.source[key] = ref[1:-1].replace('""', '"') if ref.startswith('"') else expressions.get(ref, ref)
    usage, report_filters, notes = parse_report(report_dir)
    return SemanticModel(tables, rels, usage, expressions, report_filters, notes)


SQL_RESERVED = {
    "ALL", "AND", "ANY", "AS", "ASC", "BETWEEN", "BY", "CASE", "CAST", "CHECK", "COLUMN", "CONSTRAINT", "CREATE",
    "CROSS", "CURRENT", "DATE", "DEFAULT", "DELETE", "DESC", "DISTINCT", "DROP", "ELSE", "END", "EXCEPT", "EXISTS",
    "FALSE", "FETCH", "FOR", "FOREIGN", "FROM", "FULL", "GRANT", "GROUP", "HAVING", "IN", "INNER", "INSERT",
    "INTERSECT", "INTO", "IS", "JOIN", "KEY", "LEFT", "LIKE", "LIMIT", "LEVEL", "NOT", "NULL", "OF", "OFFSET", "ON",
    "OR", "ORDER", "OUTER", "PRIMARY", "REFERENCES", "RIGHT", "ROW", "ROWS", "SELECT", "SET", "SIZE", "TABLE",
    "THEN", "TO", "TRUE", "UNION", "UNIQUE", "UPDATE", "USER", "USING", "VALUES", "WHEN", "WHERE", "WINDOW", "WITH",
}


def ident(name: str) -> str:
    """SQL identifier: plain names stay as they are (Oracle upper-case columns), others and reserved words
    (a Group or Order column in a workbook / .pbix) are quoted."""
    plain = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", name) and name.upper() not in SQL_RESERVED
    return name if plain else '"' + name.replace('"', '""') + '"'


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
    # CALCULATE filters: ((table, column), ...). The SQL refers to non-fact columns as @@table@@.column,
    # replaced by the join alias when the snapshot query is built (store.base_sql).
    filters: tuple = ()

    @property
    def tables(self) -> set[str]:
        return {t for t, _ in self.filters}

    def readable(self) -> str:
        return re.sub(r"@@(.+?)@@", r"\1", self.sql)


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
      (?P<str>"(?:[^"]|"")*")
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<colref>(?:'(?:[^']|'')*'|[A-Za-z_][\w]*)\s*\[[^\]]+\])
    | (?P<mref>\[[^\]]+\])
    | (?P<func>[A-Za-z_][\w.]*)(?=\s*\()
    | (?P<ident>'(?:[^']|'')*'|[A-Za-z_]\w*)
    | (?P<cmp><>|<=|>=|==|=|<|>)
    | (?P<logic>&&|\|\|)
    | (?P<op>[-+*/(),{}])
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
        self.cond: list[tuple] = []  # active CALCULATE filters: (table, column, op, values)

    def _col(self, ref: str) -> tuple[str, str]:
        """(DAX reference, source column) of a fact column."""
        m = re.match(r"('(?:[^']|'')*'|[A-Za-z_]\w*)\s*\[([^\]]+)\]", ref)
        table, col = unquote(m.group(1)), m.group(2)
        if table != self.fact:
            raise Unsupported(f"column {table}[{col}] is not on the fact table")
        return dax_col(table, col), self.model.tables[table].columns[col].source_column or col

    def _base(self, name: str, sql: str, distinct_of: str | None = None, dax: str = "") -> str:
        """An aggregate; inside CALCULATE the active filters are applied (CASE WHEN … / CALCULATE(…))."""
        if self.cond:
            name = f"{name}__{safe_name(self._cond_text())}"[:120]
            sql = self._conditional_sql(sql)
            dax = f"CALCULATE({dax}, {', '.join(self._cond_dax(c) for c in self.cond)})"
            filters = tuple(sorted({(c[0], c[1]) for c in self.cond}))
            self.bases.setdefault(name, Base(name, sql, distinct_of, dax, filters))
            return name
        self.bases.setdefault(name, Base(name, sql, distinct_of, dax))
        return name

    # ---- CALCULATE filters
    def _cond_text(self) -> str:
        return "_".join(f"{c[1]}_{c[2]}_{'_'.join(map(str, c[3]))}" for c in self.cond)

    def _cond_sql(self, c: tuple) -> str:
        return cond_sql(self.model, self.fact, c)

    def _cond_dax(self, c: tuple) -> str:
        return cond_dax(c)

    def _conditional_sql(self, sql: str) -> str:
        """SUM(x) → COALESCE(SUM(CASE WHEN cond THEN x END), 0); COUNT(*) → COUNT(CASE WHEN cond THEN 1 END)."""
        cond = " AND ".join(self._cond_sql(c) for c in self.cond)
        m = re.fullmatch(r"(SUM|COUNT)\((DISTINCT )?(.*)\)", sql, re.S)
        agg, distinct, arg = m.group(1), m.group(2) or "", m.group(3)
        arg = "1" if arg == "*" else arg
        out = f"{agg}({distinct}CASE WHEN {cond} THEN {arg} END)"
        return f"COALESCE({out}, 0)" if agg == "SUM" else out

    def _filters(self) -> list[tuple]:
        """CALCULATE filter arguments: Table[col] op literal, Table[col] IN {…}, NOT (…), KEEPFILTERS(…), &&."""
        out = [self._filter()]
        while self._peek() == ("logic", "&&"):
            self.i += 1
            out.append(self._filter())
        return out

    def _filter(self) -> tuple:
        kind, val = self._peek()
        if kind == "func" and val.upper() in ("KEEPFILTERS", "NOT"):
            self.i += 1
            self._take("op", "(")
            (inner,) = self._filters() if val.upper() == "KEEPFILTERS" else [self._filter()]
            self._take("op", ")")
            if val.upper() == "KEEPFILTERS":
                return inner
            table, column, op, values = inner
            flip = {"=": "<>", "==": "<>", "<>": "=", "IN": "NOT IN", "NOT IN": "IN", "<": ">=", ">=": "<",
                    ">": "<=", "<=": ">"}
            return table, column, flip[op], values
        if kind != "colref":
            raise Unsupported(f"CALCULATE filter {val!r} not supported (only column comparisons)")
        self.i += 1
        m = re.match(r"('(?:[^']|'')*'|[A-Za-z_]\w*)\s*\[([^\]]+)\]", val)
        table, column = unquote(m.group(1)), m.group(2)
        if table not in self.model.tables or column not in self.model.tables[table].columns:
            raise Unsupported(f"unknown column {val}")
        if table != self.fact:
            self.model.join_path(self.fact, table)  # must be reachable over many-to-one relationships
        nk, nv = self._peek()
        if nk == "ident" and nv.upper() == "IN":
            self.i += 1
            self._take("op", "{")
            values = [self._literal()]
            while self._peek() == ("op", ","):
                self.i += 1
                values.append(self._literal())
            self._take("op", "}")
            return table, column, "IN", tuple(values)
        if nk != "cmp":
            raise Unsupported(f"CALCULATE filter on {val} needs a comparison")
        self.i += 1
        return table, column, nv, (self._literal(),)

    def _literal(self):
        kind, val = self._take()
        if kind == "str":
            return val[1:-1].replace('""', '"')
        if kind == "num":
            return float(val) if "." in val else int(val)
        if (kind, val) == ("op", "-") and self._peek()[0] == "num":
            n = self._take()[1]
            return -(float(n) if "." in n else int(n))
        raise Unsupported(f"literal expected, got {val!r}")

    def translate(self, measure: str) -> Formula:
        if measure in self.formulas:
            return self.formulas[measure]
        m = self.model.measures[measure]
        self.tokens, self.i, self.refs, self.used = tokenize(m.expression), 0, [], []
        home, self.home = getattr(self, "home", None), m.table
        try:
            expr = self._expr()
        finally:
            self.home = home
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
                raise Unsupported(f"unknown measure {name}" + ("（欄位要放在彙總函數裡）" if self._home_column(val) else ""))
            if self.cond:  # inside CALCULATE: expand the measure under the filters (not the cached formula)
                saved = (self.tokens, self.i, getattr(self, "home", None))
                self.tokens, self.i, self.home = tokenize(self.model.measures[name].expression), 0, \
                    self.model.measures[name].table
                inner = self._expr()
                self.tokens, self.i, self.home = saved
                self.refs.append(name)
                return f"({inner})"
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
            ref, col = self._col(self._colref_token())
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
        if fn == "CALCULATE":
            saved_top = getattr(self, "_top_div", None)
            start = self.i
            depth = 0
            while True:  # skip the expression to read the filters first
                k, v = self._take()
                if (k, v) == ("op", "("):
                    depth += 1
                elif (k, v) == ("op", ")"):
                    if depth == 0:
                        raise Unsupported("CALCULATE without filters")
                    depth -= 1
                elif (k, v) == ("op", ",") and depth == 0:
                    break
            conds = self._filters()
            while self._peek() == ("op", ","):
                self.i += 1
                conds += self._filters()
            end = self._take("op", ")")
            after = self.i
            self.i = start
            self.cond += conds
            try:
                inner = self._expr()
            finally:
                del self.cond[-len(conds):]
            self._take("op", ",")
            self.i = after
            self._top_div = saved_top
            del end
            return f"({inner})"
        if fn == "IF":
            cond = self._expr()
            kind, op = self._take("cmp")
            zero = self._take("num")[1]
            self._take("op", ",")
            then = self._expr()
            if self._peek() == ("op", ","):
                self.i += 1
                self._take("func", "BLANK")
                self._take("op", "(")
                self._take("op", ")")
            self._take("op", ")")
            if op != "<>" or float(zero) != 0 or not (then.startswith("div(") and then.endswith(f", {cond})")):
                raise Unsupported("IF is only read as a guarded division: IF(x <> 0, y / x, BLANK())")
            if self.i == len(self.tokens):  # the whole measure: remember its parts, as for DIVIDE
                self._top_div = (then[4:-len(cond) - 3], cond)
            return then
        if fn == "DIVIDE":
            num = self._expr()
            self._take("op", ",")
            den = self._expr()
            self._take("op", ")")
            if self.i == len(self.tokens):  # DIVIDE is the whole measure: remember its parts
                self._top_div = (num, den)
            return f"div({num}, {den})"
        raise Unsupported(f"function {fn} not supported")

    def _home_column(self, mref: str) -> str | None:
        """[Column] without a table: a column of the measure's own table, written as 'Table'[Column]."""
        home = getattr(self, "home", None)
        if home and mref[1:-1] not in self.model.measures and mref[1:-1] in self.model.tables[home].columns:
            return f"{dax_table(home)}{mref}"
        return None

    def _colref_token(self) -> str:
        kind, val = self._take()
        if kind == "colref":
            return val
        if kind == "mref" and (ref := self._home_column(val)):
            return ref
        raise Unsupported(f"expected a column, got {val!r}")

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


def _sql_lit(v) -> str:
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'"
    return str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"


def _dax_lit(v) -> str:
    if isinstance(v, date):
        return f"DATE({v.year}, {v.month}, {v.day})"
    return str(v) if isinstance(v, (int, float)) else '"' + str(v).replace('"', '""') + '"'


def cond_sql(model: SemanticModel, fact: str, c: tuple) -> str:
    """A filter condition in SQL; columns of other tables as @@table@@.column (alias put in by store.py)."""
    table, column, op, values = c
    src = model.tables[table].columns[column].source_column or column
    col = f"f.{ident(src)}" if table == fact else f"@@{table}@@.{ident(src)}"
    if op in ("IN", "NOT IN"):
        inner = f"{col} IN ({', '.join(_sql_lit(v) for v in values)})"
        # DAX compares BLANK as not equal to any value: keep rows without a match on NOT IN / <>
        return f"({inner})" if op == "IN" else f"(NOT {inner} OR {col} IS NULL)"
    sql_op = {"==": "="}.get(op, op)
    expr = f"{col} {sql_op} {_sql_lit(values[0])}"
    return f"({expr} OR {col} IS NULL)" if op == "<>" else f"({expr})"


def cond_dax(c: tuple) -> str:
    table, column, op, values = c
    ref = dax_col(table, column)
    if op in ("IN", "NOT IN"):
        inner = f"{ref} IN {{{', '.join(_dax_lit(v) for v in values)}}}"
        return inner if op == "IN" else f"NOT ({inner})"
    return f"{ref} {op} {_dax_lit(values[0])}"


def translate_all(model: SemanticModel, fact: str) -> tuple[dict[str, Formula], dict[str, Base], dict[str, str]]:
    """Translate every measure on the fact table; returns formulas, bases and skip reasons."""
    tr = Translator(model, fact)
    skipped = {}
    # measures kept on another table are analysed when they are only parked there: the fact has no measures, or
    # the table holds no visible columns (e.g. "_Measures"), or it is not related to anything (it cannot filter);
    # a related table's measures describe that table (e.g. an ETL log) and stay out of the analysis
    fact_has = any(m.table == fact for m in model.measures.values())
    related = {r.from_table for r in model.relationships} | {r.to_table for r in model.relationships}
    parked = {t.name for t in model.tables.values()
              if not any(not c.hidden for c in t.columns.values()) or t.name not in related}
    for name, m in model.measures.items():
        if m.table != fact and fact_has and m.table not in parked:
            continue
        try:
            tr.translate(name)
        except (Unsupported, KeyError, ValueError) as e:  # ValueError: a filtered table the fact cannot reach
            tr.formulas.pop(name, None)
            skipped[name] = str(e)
    return tr.formulas, tr.bases, skipped
