"""Import a report straight from a .pbix: no Power BI project (PBIP) export needed.

    python -m analysis import-pbix REPORT.pbix --name NAME              read from the file (no Power BI needed)
    python -m analysis import-pbix REPORT.pbix --name NAME --desktop    read through Power BI Desktop instead
    python -m analysis import-pbix --title "Sales" --name NAME          an already open report (window title)

A .pbix is a zip. The report pages come from it directly (Report/Layout, or Report/definition for PBIR). The data
model is compressed; by default PBIXRay decodes it and its metadata database (the tables behind the TMSCHEMA_*
views): tables, columns, measures, relationships, Power Query partitions and parameters, and the imported data.
With --desktop (or when the file cannot be decoded, e.g. encrypted by a sensitivity label) the same metadata is
read from the engine Power BI Desktop runs while the file is open. Written to datasets/NAME/:

  powerbi/NAME.SemanticModel/definition/*.tmdl   the model, in the TMDL subset semantic.py reads
  powerbi/NAME.Report/definition/pages/...       what each visual uses, its filters, position and formatting
  analysis.yaml                                  a DRAFT of the analysis settings, derived from the data:
                                                 fact table, date, dimensions and hierarchies, metric roles,
                                                 thresholds for the data volume. Review it before use.

Snapshots then read the data stored in the .pbix (source: pbix) or, with --desktop, the open report through DAX
(source: powerbi); either way any data source the report uses works. Nothing leaves the machine.
"""
import json
import re
import subprocess
import time
import zipfile
from collections import deque
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from . import pbi
from .dataset import ROOT
from .semantic import SQL_RESERVED, dax_col, dax_table

DATA_TYPES = {"2": "string", "6": "int64", "8": "double", "9": "dateTime", "10": "decimal", "11": "boolean",
              "17": "binary"}
AUTO_TABLES = re.compile(r"^(LocalDateTable_|DateTableTemplate_)")
CODE_LIKE = re.compile(r"(?i)(^id$|_id$|\bid\b|key$|code$|代碼|編號|序號|^row ?id$)")


def q(name: str) -> str:
    """TMDL object name, quoted when needed."""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "'" + name.replace("'", "''") + "'"


def tabs(text: str, n: int) -> str:
    return "\n".join("\t" * n + line if line.strip() else "" for line in text.splitlines())


# ------------------------------------------------------------------------------------------- model
# The model's metadata: the same tables whether read from Power BI Desktop's engine (TMSCHEMA_* views) or from
# the metadata database inside the .pbix (PBIXRay).
METADATA = {
    "tables": ("Table", ["ID", "Name", "Description", "IsHidden", "DataCategory"]),
    "columns": ("Column", ["ID", "TableID", "ExplicitName", "InferredName", "SourceColumn", "ExplicitDataType",
                           "InferredDataType", "Type", "IsHidden", "Description", "FormatString", "IsKey"]),
    "measures": ("Measure", ["TableID", "Name", "Expression", "FormatString", "DisplayFolder", "Description"]),
    "relationships": ("Relationship", ["FromTableID", "FromColumnID", "ToTableID", "ToColumnID", "IsActive",
                                       "FromCardinality", "ToCardinality"]),
    "partitions": ("Partition", ["TableID", "Name", "QueryDefinition"]),
    "expressions": ("Expression", ["Name", "Expression", "Kind"]),
}
FLAGS = ("IsHidden", "IsKey", "IsActive")


def read_model(port: int) -> dict:
    """The model of a report open in Power BI Desktop (its Analysis Services engine)."""
    return build_model({k: pbi.dmv(port, f"TMSCHEMA_{v[0].upper()}S", v[1]) for k, v in METADATA.items()})


def read_model_file(pbix: Path) -> dict:
    """The model straight from the .pbix, without Power BI: PBIXRay decompresses the data model and reads the
    metadata database inside it (the tables the TMSCHEMA_* views show)."""
    src = open_pbix(pbix)._metadata.source
    # older files name some columns differently; PBIXRay knows the variants
    renamed = {"Type": src._column_type_col().split(".")[-1]} | {
        c: src._rel_col(c).split(".")[-1] for c in METADATA["relationships"][1] if c.endswith(("ID", "Cardinality"))}
    frames = {}
    for key, (table, cols) in METADATA.items():
        select = ", ".join(f"{renamed.get(c, c) if key in ('columns', 'relationships') else c} AS {c}"
                           if src._has_column(table, renamed.get(c, c) if key in ("columns", "relationships") else c)
                           else f"NULL AS {c}" for c in cols)
        where = " WHERE SystemFlags = 0" if key == "tables" else ""  # not the engine's internal tables
        df = src._db.query(f"SELECT {select} FROM [{table}]{where}").reindex(columns=cols)  # empty → no columns
        # as the DMVs give them: text, flags as True / False, None when empty
        frames[key] = pd.DataFrame({col: pd.Series([_dmv_text(v, col) for v in df[col]], dtype=object)
                                    for col in cols})
    return build_model(frames)


def _dmv_text(v, col: str):
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if col in FLAGS:
        return "True" if int(v) else "False"
    return str(int(v)) if isinstance(v, (int, float)) else v


def open_pbix(pbix: Path):
    from pbixray import PBIXRay
    return PBIXRay(str(pbix))


def build_model(f: dict) -> dict:
    # plain rows, empty values as None (pandas may hold them as NaN, which is truthy)
    t, c, m, r, p, e = ([{k: (None if isinstance(v, float) and v != v else v) for k, v in row.items()}
                         for row in f[k].to_dict("records")] for k in METADATA)
    tables = {}
    for row in t:
        if AUTO_TABLES.match(row["Name"]):
            continue  # Power BI's hidden auto date/time tables
        tables[row["ID"]] = {"name": row["Name"], "description": row["Description"] or "", "columns": {},
                             "measures": [], "partitions": [], "date_table": row["DataCategory"] == "Time"}
    columns = {}
    for row in c:
        if row["TableID"] not in tables or row["Type"] == "3":  # 3 = RowNumber
            continue
        dtype = row["ExplicitDataType"] if row["ExplicitDataType"] not in (None, "1") else row["InferredDataType"]
        col = {"name": row["ExplicitName"] or row["InferredName"], "source": row["SourceColumn"] or "",
               "type": DATA_TYPES.get(dtype, "string"), "hidden": row["IsHidden"] == "True",
               "description": row["Description"] or "", "format": row["FormatString"] or "",
               "key": row["IsKey"] == "True", "calculated": row["Type"] == "2"}
        tables[row["TableID"]]["columns"][row["ID"]] = col
        columns[row["ID"]] = (tables[row["TableID"]]["name"], col["name"])
    for row in m:
        if row["TableID"] in tables:
            tables[row["TableID"]]["measures"].append(
                {"name": row["Name"], "expression": row["Expression"] or "", "format": row["FormatString"] or "",
                 "folder": row["DisplayFolder"] or "", "description": row["Description"] or ""})
    for row in p:
        if row["TableID"] in tables:
            tables[row["TableID"]]["partitions"].append({"name": row["Name"], "m": row["QueryDefinition"] or ""})
    rels = []
    for row in r:
        if row["FromColumnID"] in columns and row["ToColumnID"] in columns:
            rels.append({"from": columns[row["FromColumnID"]], "to": columns[row["ToColumnID"]],
                         "active": row["IsActive"] == "True", "from_many": row["FromCardinality"] == "2",
                         "to_one": row["ToCardinality"] == "1"})
    exprs = [{"name": row["Name"], "expression": row["Expression"] or ""} for row in e if row["Kind"] == "0"]
    return {"tables": list(tables.values()), "relationships": rels, "expressions": exprs}


def write_model(model: dict, out: Path) -> None:
    d = out / "definition"
    if d.exists():
        for f in d.rglob("*.tmdl"):
            f.unlink()
    (d / "tables").mkdir(parents=True, exist_ok=True)
    (out / "definition.pbism").write_text(json.dumps({"version": "4.2", "settings": {}}, indent=2), encoding="utf-8")
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1601\n", encoding="utf-8")
    (d / "model.tmdl").write_text("model Model\n\tculture: zh-TW\n\n" + "".join(
        f"ref table {q(t['name'])}\n" for t in model["tables"]), encoding="utf-8")
    exprs = []
    for e in model["expressions"]:
        first, *rest = e["expression"].splitlines() or [""]
        exprs.append(f"expression {q(e['name'])} = {first}" + ("\n" + tabs("\n".join(rest), 2) if rest else "") + "\n")
    (d / "expressions.tmdl").write_text("\n".join(exprs), encoding="utf-8")
    (d / "relationships.tmdl").write_text("\n".join(
        f"relationship r{i}\n"
        + ("" if r["active"] else "\tisActive: false\n")
        + ("" if r["from_many"] else "\tfromCardinality: one\n")
        + ("" if r["to_one"] else "\ttoCardinality: many\n")
        + f"\tfromColumn: {q(r['from'][0])}.{q(r['from'][1])}\n\ttoColumn: {q(r['to'][0])}.{q(r['to'][1])}\n"
        for i, r in enumerate(model["relationships"])), encoding="utf-8")
    for t in model["tables"]:
        lines = ([f"/// {t['description']}"] if t["description"] else []) + [f"table {q(t['name'])}", ""]
        for m in t["measures"]:
            if m["description"]:
                lines.append(f"\t/// {m['description']}")
            expr = m["expression"].strip()
            if "\n" in expr:
                lines += [f"\tmeasure {q(m['name'])} = ```", tabs(expr, 3), "\t\t\t```"]
            else:
                lines.append(f"\tmeasure {q(m['name'])} = {expr}")
            lines += [f"\t\tformatString: {m['format']}"] if m["format"] else []
            lines += [f"\t\tdisplayFolder: {m['folder']}"] if m["folder"] else []
            lines.append("")
        for c in t["columns"].values():
            if c["description"]:
                lines.append(f"\t/// {c['description']}")
            lines += [f"\tcolumn {q(c['name'])}", f"\t\tdataType: {c['type']}"]
            lines += ["\t\tisHidden"] if c["hidden"] else []
            lines += [f"\t\tformatString: {c['format']}"] if c["format"] else []
            lines += [f"\t\tsourceColumn: {c['source']}"] if c["source"] else []
            lines.append("")
        for p in t["partitions"]:
            lines += [f"\tpartition {q(p['name'])} = m", "\t\tmode: import", "\t\tsource =", tabs(p["m"], 4), ""]
        safe = re.sub(r'[\\/:*?"<>|]', "_", t["name"])
        (d / "tables" / f"{safe}.tmdl").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------------------------------- report
def _entity_field(sel: dict, alias: dict) -> dict | None:
    """A prototypeQuery select (Measure / Column / Aggregation over a column) with the alias replaced by the table."""
    for kind in ("Measure", "Column"):
        if kind in sel:
            entity = alias.get(sel[kind].get("Expression", {}).get("SourceRef", {}).get("Source"))
            if entity is None:
                return None
            return {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": sel[kind]["Property"]}}
    if "Aggregation" in sel:
        inner = _entity_field(sel["Aggregation"].get("Expression", {}), alias)
        return {"Aggregation": {"Expression": inner, "Function": sel["Aggregation"].get("Function")}} if inner else None
    return None


def write_report(pbix: Path | None, out: Path) -> int:
    """Copy the report into a minimal PBIR folder: which fields each visual uses, its filters, position and
    formatting (for intent.py); returns the number of visuals."""
    pages_dir = out / "definition" / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    if pbix is None:  # only the open report: keep pages imported earlier from the file, if any
        if not (pages_dir / "pages.json").exists():
            (pages_dir / "pages.json").write_text(json.dumps({"pageOrder": []}), encoding="utf-8")
        return 0
    with zipfile.ZipFile(pbix) as z:
        names = z.namelist()
        if any(n.startswith("Report/definition/") for n in names):  # PBIR inside the .pbix: copy as is
            for n in names:
                if n.startswith("Report/definition/") and not n.endswith("/"):
                    target = out / n[len("Report/"):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(n))
            return sum(1 for n in names if n.endswith("/visual.json"))
        layout = json.loads(z.read("Report/Layout").decode("utf-16-le"))
    filters = lambda obj: {"filterConfig": {"filters": json.loads(obj.get("filters") or "[]")}}  # noqa: E731
    (out / "definition" / "report.json").write_text(json.dumps(filters(layout), ensure_ascii=False), encoding="utf-8")
    order, count = [], 0
    for sec in layout.get("sections", []):
        name = re.sub(r"\W", "_", sec.get("name") or f"page{len(order)}")
        order.append(name)
        pdir = pages_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        page = {"name": name, "displayName": sec.get("displayName", name), "width": sec.get("width", 1280),
                "height": sec.get("height", 720), **filters(sec)}
        if json.loads(sec.get("config") or "{}").get("visibility") == 1:
            page["visibility"] = "HiddenInViewMode"
        (pdir / "page.json").write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
        for i, vc in enumerate(sec.get("visualContainers", [])):
            cfg = json.loads(vc.get("config") or "{}")
            sv = cfg.get("singleVisual") or {}
            query = sv.get("prototypeQuery") or {}
            alias = {f["Name"]: f["Entity"] for f in query.get("From", [])}
            by_ref = {}
            for sel in query.get("Select", []):
                field = _entity_field(sel, alias)
                if field:
                    by_ref[sel.get("Name")] = {"field": field}
            if not sv.get("visualType"):
                continue
            # fields per role (Y, Category, TargetValue …), as in PBIR; fields in no role go under Values
            roles = {r: [by_ref[p["queryRef"]] for p in ps if p.get("queryRef") in by_ref]
                     for r, ps in (sv.get("projections") or {}).items()}
            placed = {json.dumps(f) for fs in roles.values() for f in fs}
            rest = [f for f in by_ref.values() if json.dumps(f) not in placed]
            if rest:
                roles.setdefault("Values", []).extend(rest)
            # keep the author's formatting (conditional formats, lines, colours) and the position for intent.py
            visual = {"name": cfg.get("name", f"v{i}"),
                      "position": {"x": vc.get("x", 0), "y": vc.get("y", 0), "width": vc.get("width", 0),
                                   "height": vc.get("height", 0)},
                      "visual": {"visualType": sv.get("visualType", ""),
                                 "query": {"queryState": {r: {"projections": fs} for r, fs in roles.items() if fs}},
                                 "objects": sv.get("objects") or {},
                                 "visualContainerObjects": sv.get("vcObjects") or {}},
                      **filters(vc)}
            vdir = pdir / "visuals" / visual["name"]
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "visual.json").write_text(json.dumps(visual, ensure_ascii=False), encoding="utf-8")
            count += 1
    bookmarks = json.loads(layout.get("config") or "{}").get("bookmarks", [])
    bookmarks += [c for b in bookmarks for c in b.get("children", [])]
    for i, b in enumerate(b for b in bookmarks if b.get("displayName")):
        bdir = out / "definition" / "bookmarks"
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / f"b{i:03d}.bookmark.json").write_text(json.dumps({"displayName": b["displayName"]}, ensure_ascii=False),
                                                  encoding="utf-8")
    (pages_dir / "pages.json").write_text(json.dumps({"pageOrder": order}), encoding="utf-8")
    return count


# ------------------------------------------------------------------------------------------- draft config
ROLE_NAMES = {
    "sales": r"^(銷售額|營收|營業額|銷售金額|業績|sales|revenue|total sales)$",
    "volume": r"^(銷售數量|銷量|數量|件數|quantity|qty|units)$",
    "orders": r"^(訂單數|訂單量|交易數|來客數|orders|order count|transactions)$",
    "unit_price": r"平均.*單價|平均.*標價|平均售價|avg.*price|average.*price",
    "discount_rate": r"折扣率|discount ?(rate|%)",
    "margin_rate": r"^(毛利率|利潤率|margin|gross margin|profit margin)( ?%)?$",
}


def reachable(model: dict, fact: str) -> dict[str, list]:
    """Tables reachable from the fact over active many-to-one relationships."""
    seen, queue = {fact: []}, deque([fact])
    while queue:
        t = queue.popleft()
        for r in model["relationships"]:
            if r["active"] and r["from_many"] and r["to_one"] and r["from"][0] == t and r["to"][0] not in seen:
                seen[r["to"][0]] = seen[t] + [r]
                queue.append(r["to"][0])
    return seen


def slug(name: str, used: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "dim"
    if base.upper() in SQL_RESERVED or base[0].isdigit():  # keys become column names in the snapshot SQL
        base = f"{base}_dim"
    key, i = base, 2
    while key in used:
        key, i = f"{base}_{i}", i + 1
    used.add(key)
    return key


class DaxProbe:
    """Questions about the data, answered with DAX by the report open in Power BI Desktop."""

    def __init__(self, port: int):
        self.port = port

    def stats(self, fact: str, date_ref: tuple, cands: list[tuple]) -> tuple[float, float, dict]:
        exprs = [f'"c{i}", DISTINCTCOUNT({dax_col(t, c)})' for i, (t, c) in enumerate(cands)]
        rows_dax = f"COUNTROWS({dax_table(fact)})"
        days_dax = f"COUNTROWS(FILTER(VALUES({dax_col(*date_ref)}), NOT ISBLANK(CALCULATE({rows_dax}))))"
        res = pbi.run_dax(self.port, [f'EVALUATE ROW("rows", {rows_dax}, "days", {days_dax}'
                                      f'{", " if exprs else ""}{", ".join(exprs)})'])[0]
        if "error" in res:
            raise SystemExit(res["error"])
        values = [float(v or 0) for v in res["rows"][0]]
        return values[0], values[1], {cands[i]: int(v) for i, v in enumerate(values[2:])}

    def pair_counts(self, pairs: list[tuple]) -> list[int | None]:
        res = pbi.run_dax(self.port, ["EVALUATE ROW(" + ", ".join(
            f'"p{i}", COUNTROWS(SUMMARIZE({dax_table(a[0])}, {dax_col(*a)}, {dax_col(*b)}))'
            for i, (a, b) in enumerate(pairs)) + ")"])[0]
        return [int(float(n or 0)) for n in res["rows"][0]] if "rows" in res else [None] * len(pairs)

    def daily_minimum(self, fact: str, date_ref: tuple, measures: list[str]) -> dict[str, float | None]:
        res = pbi.run_dax(self.port, ["EVALUATE ROW(" + ", ".join(
            f'"m{i}", MINX(VALUES({dax_col(*date_ref)}), [{m}])' for i, m in enumerate(measures)) + ")"])[0]
        if "rows" not in res:
            return {}
        return {m: (None if v is None else float(v)) for m, v in zip(measures, res["rows"][0])}


class FileProbe:
    """The same questions answered from the tables stored in the .pbix (PBIXRay → DuckDB) and the model already
    written as TMDL: measures are evaluated per day through their translation to SQL (semantic.py)."""

    def __init__(self, pbix: Path, pbip_dir: Path):
        import duckdb
        from . import semantic, sources
        self.semantic = semantic
        self.model = semantic.load(pbip_dir)
        self.db = duckdb.connect()
        px = open_pbix(pbix)
        for t in self.model.tables.values():
            sources.register_table(self.db, px.get_table(t.name), self.src(t.name),
                                   {c.name: c.source_column for c in t.columns.values() if c.source_column})

    def src(self, table: str) -> str:
        return self.model.tables[table].source_table or table

    def col(self, table: str, column: str) -> str:
        c = self.model.tables[table].columns[column]
        return self.semantic.ident(c.source_column or column)

    def one(self, sql: str):
        return self.db.execute(sql).fetchone()

    def joins(self, fact: str, tables: set[str]) -> tuple[str, dict[str, str]]:
        """FROM fact LEFT JOIN … along the many-to-one paths to the given tables (as store.py joins)."""
        ident = self.semantic.ident
        alias, sql = {fact: "f"}, f"FROM {ident(self.src(fact))} f"
        for target in sorted(tables - {fact}):
            for r in self.model.join_path(fact, target):
                if r.to_table not in alias:
                    alias[r.to_table] = f"t{len(alias)}"
                    sql += (f" LEFT JOIN {ident(self.src(r.to_table))} {alias[r.to_table]} ON "
                            f"{alias[r.from_table]}.{self.col(r.from_table, r.from_column)} = "
                            f"{alias[r.to_table]}.{self.col(r.to_table, r.to_column)}")
        return sql, alias

    def stats(self, fact: str, date_ref: tuple, cands: list[tuple]) -> tuple[float, float, dict]:
        ident = self.semantic.ident
        rows = self.one(f"SELECT COUNT(*) FROM {ident(self.src(fact))}")[0]
        frm, alias = self.joins(fact, {date_ref[0]})
        days = self.one(f"SELECT COUNT(DISTINCT {alias[date_ref[0]]}.{self.col(*date_ref)}) {frm}")[0]
        card = {(t, c): self.one(f"SELECT COUNT(DISTINCT {self.col(t, c)}) FROM {ident(self.src(t))}")[0]
                for t, c in cands}
        return float(rows), float(days), card

    def pair_counts(self, pairs: list[tuple]) -> list[int | None]:
        ident = self.semantic.ident
        return [self.one(f"SELECT COUNT(*) FROM (SELECT DISTINCT {self.col(*a)}, {self.col(*b)} "
                         f"FROM {ident(self.src(a[0]))})")[0] for a, b in pairs]

    def daily_minimum(self, fact: str, date_ref: tuple, measures: list[str]) -> dict[str, float | None]:
        """Lowest daily value of each measure that translates to SQL (the others are left out)."""
        formulas, bases, _ = self.semantic.translate_all(self.model, fact)
        wanted = [m for m in measures if m in formulas]
        if not wanted:
            return {}
        used = sorted({b for m in wanted for b in formulas[m].bases})
        frm, alias = self.joins(fact, {date_ref[0]} | {t for b in used for t in bases[b].tables})
        aggs = [re.sub(r"@@(.+?)@@", lambda x: alias[x.group(1)], bases[b].sql) + f' AS "{b}"' for b in used]
        day = f"{alias[date_ref[0]]}.{self.col(*date_ref)}"
        df = self.db.execute(f"SELECT {day} AS d, {', '.join(aggs)} {frm} WHERE {day} IS NOT NULL GROUP BY {day}").df()
        div = lambda a, b: a / b if b else None  # noqa: E731
        out = {}
        for m in wanted:
            values = []
            for row in df.to_dict("records"):
                try:
                    v = eval(formulas[m].expr, {"div": div}, {k: float(x or 0) for k, x in row.items() if k != "d"})
                except TypeError:  # a division by zero inside the formula (DAX gives BLANK)
                    v = None
                if v is not None:
                    values.append(v)
            out[m] = min(values) if values else None
        return out


def draft_config(model: dict, probe, title: str, source: dict) -> tuple[dict, list[str]]:
    """Analysis settings inferred from the model and the data; probe answers the questions about the data
    (DaxProbe: the open report's engine; FileProbe: the tables inside the .pbix)."""
    notes = []
    by_name = {t["name"]: t for t in model["tables"]}
    many = {t["name"]: sum(1 for r in model["relationships"] if r["from"][0] == t["name"] and r["from_many"])
            for t in model["tables"]}
    # the table the measures aggregate (Table[Column] in their DAX, or a bare [Column] of the measure's own table):
    # in models without relationships, or with measures kept on another table, this is the fact
    aggregated = {t["name"]: 0 for t in model["tables"]}
    for t in model["tables"]:
        for m in t["measures"]:
            for table, col in re.findall(r"(?:'((?:[^']|'')+)'|\b([A-Za-z_][\w]*))\s*\[", m["expression"]):
                name = (table or col).replace("''", "'")
                if name in aggregated:
                    aggregated[name] += 1
            own = {c["name"] for c in t["columns"].values()}
            aggregated[t["name"]] += sum(1 for ref in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", m["expression"])
                                         if ref in own)
    fact = max(model["tables"], key=lambda t: (many[t["name"]], aggregated[t["name"]], len(t["measures"]),
                                               len(t["columns"])))["name"]
    tables = reachable(model, fact)
    notes.append(f"事實表：{fact}（多對一關聯 {many[fact]} 條、量值 {len(by_name[fact]['measures'])} 個）")

    # date: a marked date table's key, else the first date column on the fact
    date_ref = None
    for t in tables:
        tt = by_name[t]
        if tt["date_table"]:
            col = next((c for c in tt["columns"].values() if c["key"] and c["type"] == "dateTime"),
                       next((c for c in tt["columns"].values() if c["type"] == "dateTime"), None))
            if col:
                date_ref = (t, col["name"])
    if not date_ref:
        for t in tables:
            col = next((c for c in by_name[t]["columns"].values() if c["type"] == "dateTime" and not c["hidden"]), None)
            if col:
                date_ref = (t, col["name"])
                break
    if not date_ref:
        raise SystemExit("找不到日期欄位：分析需要一個與事實表相關的日期欄位")
    notes.append(f"日期：{date_ref[0]}[{date_ref[1]}]")

    # candidate dimensions: visible text columns on reachable tables (not ids/codes), sized by cardinality
    cands = [(t, c["name"]) for t in tables for c in by_name[t]["columns"].values()
             if c["type"] == "string" and not c["hidden"] and not CODE_LIKE.search(c["name"])
             and (t != date_ref[0] or t == fact)]  # a date table is no dimension; the fact itself may hold the date
    rows, days, card = probe.stats(fact, date_ref, cands)
    rows_per_day = rows / max(days, 1)
    dims = [(t, c) for (t, c), n in card.items() if 2 <= n <= 200]
    item = max(((t, c) for (t, c), n in card.items() if 200 < n <= 20000), key=lambda x: card[x], default=None)
    notes.append(f"資料量：平均每天 {rows_per_day:,.0f} 列明細")
    notes.append("維度候選（成員數）：" + "、".join(f"{c}({card[(t, c)]})" for t, c in dims))
    skipped = [f"{c}({n})" for (t, c), n in card.items() if n > 200 and (t, c) != item]
    if skipped:
        notes.append("成員太多、沒有列為維度：" + "、".join(skipped))

    # hierarchies on one table: b's parent is a when every b value has exactly one a
    pairs = [(a, b) for a in dims for b in dims if a[0] == b[0] and card[a] < card[b]]
    parent = {}
    if pairs:
        for (a, b), n in zip(pairs, probe.pair_counts(pairs)):
            if n == card[b] and (b not in parent or card[a] > card[parent[b]]):
                parent[b] = a
    used: set = set()
    keys = {d: slug(d[1], used) for d in dims}
    def label(t: str, c: str) -> str:
        """Short label from the column description ("區域：北區 / 中區" → 區域), else the column name."""
        desc = next((x["description"] for x in by_name[t]["columns"].values() if x["name"] == c), "")
        short = re.split(r"[：:（(，,。]", desc)[0].strip()
        return short if 0 < len(short) <= 8 else c

    dimensions = {}
    for d in dims:
        spec = {"label": label(*d), "column": f"{d[0]}.{d[1]}"}
        if d in parent:
            spec["parent"] = keys[parent[d]]
        dimensions[keys[d]] = spec
    if item:
        k = slug(item[1], used)
        dimensions[k] = {"label": label(*item), "column": f"{item[0]}.{item[1]}", "scan": False}
        notes.append(f"品項（只用於同品項售價指數，不掃描）：{item[1]}（{card[item]}）")

    # metric roles from measure names; metrics that go negative are 'signed'
    measures = [m["name"] for m in by_name[fact]["measures"]]
    amounts = [m["name"] for m in by_name[fact]["measures"] if "%" not in m["format"]]  # % changes: not amounts
    roles = {}
    for role, pattern in ROLE_NAMES.items():
        hit = next((m for m in measures if re.search(pattern, m, re.I)), None)
        if hit:
            roles[role] = hit
    lowest = probe.daily_minimum(fact, date_ref, amounts) if amounts else {}
    signed = [m for m in amounts if lowest.get(m) is not None and lowest[m] < 0]
    unchecked = [m for m in amounts if m not in lowest]
    if unchecked:
        notes.append("無法逐日計算、沒有檢查是否可能為負：" + "、".join(unchecked))
    if signed:
        roles["signed"] = signed
    notes.append("指標角色：" + "、".join(f"{k}={v}" for k, v in roles.items() if k != "signed")
                 + (f"；可能為負：{'、'.join(signed)}" if signed else ""))

    small = rows_per_day < 300
    roots = [keys[d] for d in dims if d not in parent][:4]
    config = {
        "description": f"{title}（由 .pbix 匯入，" + ("資料直接從 .pbix 檔讀取）" if source["kind"] == "pbix"
                                                   else "資料從開啟中的 Power BI 報表讀取）"),
        "source": source,
        "fact": fact,
        "date": f"{date_ref[0]}.{date_ref[1]}",
        "dimensions": dimensions,
        "combinations": [[a, b] for i, a in enumerate(roots) for b in roots[i + 1:]][:4],
        "windows": ({"week": {"days": 7, "label": "近 7 日", "seasonal": "total"},
                     "month": {"days": 28, "label": "近 28 日", "seasonal": "total"},
                     "year": {"days": 28, "label": "近 28 日 vs 去年同期", "mode": "yoy"}} if small else
                    {"day": {"days": 1, "label": "單日"}, "week": {"days": 7, "label": "近 7 日"},
                     "month": {"days": 28, "label": "近 28 日"},
                     "year": {"days": 28, "label": "近 28 日 vs 去年同期", "mode": "yoy"}}),
        "roles": roles,
        "thresholds": ({"history_days": 364, "min_rows": 60, "z_alert": 3.0, "z_alert_member": 5.0,
                        "pattern_min_z": 2.0, "price_index_move": 0.08} if small else
                       {"history_days": 182, "z_alert": 3.0, "z_alert_member": 4.0})
        | {"min_change_pct": 0.05, "min_share": 0.01, "min_ratio_change": 0.01, "max_findings": 8, "repeat_days": 14},
    }
    if small:
        config["default_window"] = "week"
        notes.append("資料量小：採用小量資料的門檻（不比較單日、至少 60 筆明細才判斷、成員 z ≥ 5），請用沒有事件的歷史資料校準")
    if "unit_price" in roles and (item or dims):
        # the finest product-like dimension: a price index over it is not moved by product mix
        config["price_item"] = (next(k for k, v in dimensions.items() if v.get("scan") is False) if item
                                else keys[max(dims, key=lambda d: card[d])])
    if not small:
        # data-quality unit: a day where one of these suddenly has far fewer rows is flagged as a possible data gap.
        # The most detailed dimension outside the product table (stores, branches, …).
        item_table = dimensions.get(config.get("price_item"), {}).get("column", ".").split(".")[0]
        units = [d for d in dims if d[0] != item_table and card[d] <= 100]
        if units:
            unit = max(units, key=lambda d: card[d])
            config["data_quality"] = {"unit": keys[unit]}
            notes.append(f"資料品質檢查單位：{label(*unit)}（某天明細筆數突然低於平常一半時提示可能是資料缺漏）")
    holiday = next((c["name"] for c in by_name[date_ref[0]]["columns"].values()
                    if re.search(r"(?i)holiday|節日|假日", c["name"])), None)
    if holiday:
        config["holiday"] = f"{date_ref[0]}.{holiday}"
    return config, notes


# ------------------------------------------------------------------------------------------- entry point
def open_report(pbix: Path, wait: int = 300) -> tuple[int, str]:
    title = pbix.stem
    found = [e for e in pbi.engines() if (e.get("title") or "").startswith(title)]
    if not found:
        subprocess.run(["powershell", "-NoProfile", "-Command", f'Start-Process "{pbix}"'], check=True)
        t0 = time.time()
        while time.time() - t0 < wait and not found:
            time.sleep(5)
            found = [e for e in pbi.engines() if (e.get("title") or "").startswith(title) and e.get("port")]
        if not found:
            raise SystemExit(f"Power BI Desktop 沒有在 {wait} 秒內開啟 {pbix.name}")
        time.sleep(10)  # let the model finish loading
    return int(found[0]["port"]), title


def main(file: str | None, name: str, port: int | None, title: str | None, desktop: bool = False) -> None:
    pbix = Path(file).resolve() if file else None
    home = ROOT / "datasets" / name
    out = home / "powerbi"
    model = None
    if pbix and port is None and not title and not desktop:
        try:  # straight from the file: no Power BI needed
            model = read_model_file(pbix)
            source = {"kind": "pbix", "file": str(pbix)}
            print(f"read from the file (PBIXRay): {pbix.name}")
        except Exception as e:  # encrypted (sensitivity label), a format PBIXRay does not know …
            print(f"cannot read {pbix.name} directly ({type(e).__name__}: {e}); opening it in Power BI Desktop")
    if model is None:
        if port is None:
            if pbix:
                port, title = open_report(pbix)
            else:
                port = pbi.find_port(title)
        model = read_model(port)
        source = {"kind": "powerbi", "title": title or (pbix.stem if pbix else name)}
    title = title or (pbix.stem if pbix else name)
    write_model(model, out / f"{name}.SemanticModel")
    visuals = write_report(pbix, out / f"{name}.Report")
    print(f"model: {len(model['tables'])} tables, {sum(len(t['measures']) for t in model['tables'])} measures, "
          f"{len(model['relationships'])} relationships; report: {visuals} visuals")
    probe = FileProbe(pbix, out) if source["kind"] == "pbix" else DaxProbe(port)
    config, notes = draft_config(model, probe, title, source)
    target = home / "analysis.yaml"
    if target.exists():
        target = home / "analysis.draft.yaml"
        print(f"{home / 'analysis.yaml'} already exists: draft written to {target.name}")
    header = ("# 由 python -m analysis import-pbix 自動產生的分析設定草稿（" + str(date.today()) + "）。\n"
              "# 依資料推斷，請檢查：維度與標籤、階層（parent）、指標角色（roles）、門檻（用沒有事件的歷史資料校準）。\n"
              + "".join(f"#   - {n}\n" for n in notes) + "\n")
    target.write_text(header + yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    print("\n".join(notes))
    print(f"written: {out}, {target}")
    print(f"next: set PBI_DATASET={name}, then python -m analysis model / snapshot")
