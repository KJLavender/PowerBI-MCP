"""Build a Power BI Report Server compatible .pbix from the PBIR project.

Report Server (1.27, Sept 2026) only understands the legacy report format (a single Report/Layout
document); PBIR (Report/definition/...) uploads fail with "There was an error uploading your .pbix".
This script converts the PBIR pages/visuals into a legacy Layout and swaps it into a .pbix saved by
Power BI Desktop for Report Server (which carries the data model).

    python powerbi/to_legacy_pbix.py [base.pbix] [out.pbix]
"""
import json
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PBIR = HERE / "RetailSales.Report" / "definition"
OUT = HERE / "RetailSales.pbix"
# data model source: a fresh save from Power BI Desktop for Report Server if present, else the published file
BASE = HERE / "RetailSales_RS.pbix" if (HERE / "RetailSales_RS.pbix").exists() else OUT
# parts that are regenerated (report) or must not be redistributed (encrypted data source credentials)
DROP = ("Report/definition/", "Report/Layout", "SecurityBindings")
LAYOUT_VERSION = "5.43"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def legacy_visual(v: dict) -> dict:
    """PBIR visual.json -> legacy visualContainer."""
    pos = v["position"]
    vis = v["visual"]
    single = {"visualType": vis["visualType"], "drillFilterOtherVisuals": vis.get("drillFilterOtherVisuals", True)}
    query = vis.get("query")
    if query:
        aliases, sources, select, projections = {}, [], [], {}
        for role, state in query["queryState"].items():
            projections[role] = []
            for p in state["projections"]:
                kind, body = next(iter(p["field"].items()))
                entity = body["Expression"]["SourceRef"]["Entity"]
                if entity not in aliases:
                    base = entity[0].lower()
                    alias = base
                    n = 1
                    while alias in aliases.values():
                        alias = f"{base}{n}"
                        n += 1
                    aliases[entity] = alias
                    sources.append({"Name": alias, "Entity": entity, "Type": 0})
                ref = p["queryRef"]
                if not any(s["Name"] == ref for s in select):
                    select.append({kind: {"Expression": {"SourceRef": {"Source": aliases[entity]}}, "Property": body["Property"]},
                                   "Name": ref, "NativeReferenceName": p.get("nativeQueryRef", body["Property"])})
                proj = {"queryRef": ref}
                if p.get("active"):
                    proj["active"] = True
                projections[role].append(proj)
        proto = {"Version": 2, "From": sources, "Select": select}
        sort = query.get("sortDefinition", {}).get("sort", [])
        if sort:
            order = []
            for s in sort:
                kind, body = next(iter(s["field"].items()))
                entity = body["Expression"]["SourceRef"]["Entity"]
                order.append({"Direction": 1 if s.get("direction", "Ascending") == "Ascending" else 2,
                              "Expression": {kind: {"Expression": {"SourceRef": {"Source": aliases[entity]}},
                                                    "Property": body["Property"]}}})
            proto["OrderBy"] = order
        single["projections"] = projections
        single["prototypeQuery"] = proto
    if vis.get("objects"):
        single["objects"] = vis["objects"]
    if vis.get("visualContainerObjects"):
        single["vcObjects"] = vis["visualContainerObjects"]
    config = {"name": v["name"],
              "layouts": [{"id": 0, "position": {k: pos[k] for k in ("x", "y", "z", "width", "height", "tabOrder") if k in pos}}],
              "singleVisual": single}
    return {"x": pos["x"], "y": pos["y"], "z": pos.get("z", 0), "width": pos["width"], "height": pos["height"],
            "config": json.dumps(config, ensure_ascii=False), "filters": "[]"}


def legacy_layout() -> dict:
    report = load(PBIR / "report.json")
    order = load(PBIR / "pages" / "pages.json")["pageOrder"]
    theme = report["themeCollection"]["baseTheme"]["name"]
    sections = []
    for i, name in enumerate(order):
        page = load(PBIR / "pages" / name / "page.json")
        visuals = [legacy_visual(load(f)) for f in sorted((PBIR / "pages" / name / "visuals").glob("*/visual.json"))]
        sections.append({"name": name, "displayName": page["displayName"], "displayOption": 1,
                         "width": page["width"], "height": page["height"], "ordinal": i,
                         "config": "{}", "filters": "[]", "visualContainers": visuals})
    config = {"version": LAYOUT_VERSION,
              "themeCollection": {"baseTheme": {"name": theme, "version": LAYOUT_VERSION, "type": 2}},
              "activeSectionIndex": 0, "defaultDrillFilterOtherVisuals": True,
              "settings": {"useStylableVisualContainerHeader": True, "defaultFilterActionIsDataFilter": True,
                           "useEnhancedTooltips": True}}
    return {"id": 0,
            "resourcePackages": [{"resourcePackage": {"name": "SharedResources", "type": 2, "disabled": False,
                                                      "items": [{"name": theme, "path": f"BaseThemes/{theme}.json",
                                                                 "type": 202}]}}],
            "sections": sections, "config": json.dumps(config, ensure_ascii=False),
            "layoutOptimization": 0, "filters": "[]"}


def build(base: Path = BASE, out: Path = OUT) -> Path:
    layout = json.dumps(legacy_layout(), ensure_ascii=False).encode("utf-16-le")
    tmp = out.with_suffix(".tmp")
    with zipfile.ZipFile(base) as src, zipfile.ZipFile(tmp, "w") as dst:
        for item in src.infolist():
            if item.filename.startswith(DROP):
                continue
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                text = data.decode("utf-8-sig").replace('<Override PartName="/SecurityBindings" ContentType="" />', "")
                if "/Report/Layout" not in text:
                    text = text.replace("</Types>", '<Override PartName="/Report/Layout" ContentType="" /></Types>')
                data = text.encode("utf-8-sig")
            info = zipfile.ZipInfo(item.filename, date_time=item.date_time)
            info.compress_type = item.compress_type
            dst.writestr(info, data)
        info = zipfile.ZipInfo("Report/Layout")
        info.compress_type = zipfile.ZIP_DEFLATED
        dst.writestr(info, layout)
    tmp.replace(out)
    return out


if __name__ == "__main__":
    args = [Path(a) for a in sys.argv[1:]]
    path = build(*args)
    print(f"written {path} ({path.stat().st_size:,} bytes)")
