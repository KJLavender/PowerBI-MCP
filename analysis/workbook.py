"""An Excel workbook as a model of plain tables, so it takes part in the catalog next to the reports.

    python -m analysis import-excel BOOK.xlsx --name NAME

Every Excel table object becomes a table (reports usually read those: Source{[Item="Sales", Kind="Table"]}),
and every sheet without one that has a header row; columns get the type pandas reads.
There are no measures and no relationships: the workbook is not analysed on its own, it is compared with the
reports (same file and sheet as a report's source, codes that match or do not match a report's columns).
Written to datasets/NAME/powerbi/NAME.SemanticModel (TMDL with an Excel.Workbook partition per sheet) and an
empty NAME.Report, the same layout as the reports, so the catalog reads it like any other dataset.
"""
import re
from pathlib import Path

import pandas as pd

from .dataset import ROOT

TYPES = {"i": ("Int64.Type", "int64"), "u": ("Int64.Type", "int64"), "f": ("type number", "double"),
         "b": ("type logical", "boolean"), "M": ("type datetime", "dateTime")}


def q(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "'" + name.replace("'", "''") + "'"


def m_text(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def sheets(path: Path) -> dict[str, tuple[str, pd.DataFrame]]:
    """name → (Kind, first rows): Excel table objects, and sheets without one that have a header row
    (2,000 rows are enough to type the columns)."""
    import openpyxl
    from openpyxl.utils import range_boundaries
    book = openpyxl.load_workbook(path, read_only=False, data_only=True)
    out = {}
    for ws in book.worksheets:
        if ws.tables:
            for name, ref in ws.tables.items():
                c0, r0, c1, r1 = range_boundaries(ref if isinstance(ref, str) else ref.ref)
                df = pd.read_excel(path, sheet_name=ws.title, header=r0 - 1, usecols=range(c0 - 1, c1),
                                   nrows=min(2000, r1 - r0))
                out[name] = ("Table", df)
            continue
        df = pd.read_excel(path, sheet_name=ws.title, nrows=2000).dropna(axis=1, how="all")
        if len(df) and not all(str(c).startswith("Unnamed") for c in df.columns):
            out[ws.title] = ("Sheet", df)
    return out


def import_workbook(path: Path, name: str, title: str | None = None, root: Path | None = None) -> Path:
    path = Path(path).resolve()
    title = title or path.stem
    home = (root or ROOT / "datasets") / name
    out = home / "powerbi"
    model = out / f"{name}.SemanticModel" / "definition"
    (model / "tables").mkdir(parents=True, exist_ok=True)
    for f in (model / "tables").glob("*.tmdl"):
        f.unlink()
    found = sheets(path)
    if not found:
        raise SystemExit(f"{path.name} 沒有可讀的工作表（第一列要是欄位名稱）")
    (out / f"{name}.SemanticModel" / "definition.pbism").write_text('{"version": "4.2", "settings": {}}', encoding="utf-8")
    (model / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1601\n", encoding="utf-8")
    (model / "model.tmdl").write_text("model Model\n\tculture: zh-TW\n\n" + "".join(
        f"ref table {q(s)}\n" for s in found), encoding="utf-8")
    (model / "relationships.tmdl").write_text("", encoding="utf-8")
    (model / "expressions.tmdl").write_text(
        f"expression WorkbookPath = {m_text(str(path))} meta [IsParameterQuery=true, Type=\"Text\"]\n", encoding="utf-8")
    for sheet, (kind, df) in found.items():
        cols = [(str(c), TYPES.get(df[c].dtype.kind, ("type text", "string"))) for c in df.columns]
        typed = ", ".join(f"{{{m_text(c)}, {mt}}}" for c, (mt, _) in cols)
        what = "Excel 表格" if kind == "Table" else "工作表"
        lines = [f"/// {what} {sheet}（{path.name}）", f"table {q(sheet)}", ""]
        for c, (_, dtype) in cols:
            lines += [f"\tcolumn {q(c)}", f"\t\tdataType: {dtype}", f"\t\tsourceColumn: {c}", ""]
        m = (f"let\n    Source = Excel.Workbook(File.Contents(WorkbookPath), null, true),\n"
             f"    Item = Source{{[Item = {m_text(sheet)}, Kind = \"{kind}\"]}}[Data],\n"
             + ("    Promoted = Table.PromoteHeaders(Item, [PromoteAllScalars = true]),\n" if kind == "Sheet"
                else "    Promoted = Item,\n")  # an Excel table carries its header
             + f"    Typed = Table.TransformColumnTypes(Promoted, {{{typed}}})\nin\n    Typed")
        lines += [f"\tpartition {q(sheet)} = m", "\t\tmode: import", "\t\tsource ="]
        lines += ["\t\t\t\t" + x for x in m.splitlines()] + [""]
        safe = re.sub(r'[\\/:*?"<>|]', "_", sheet)
        (model / "tables" / f"{safe}.tmdl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pages = out / f"{title}.Report" / "definition" / "pages"
    for old in out.glob("*.Report"):
        if old.name != f"{title}.Report":
            import shutil
            shutil.rmtree(old)
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "pages.json").write_text('{"pageOrder": []}', encoding="utf-8")
    (home / "workbook.txt").write_text(str(path), encoding="utf-8")
    print(f"{path.name}: {len(found)} sheets → {out}")
    return out
