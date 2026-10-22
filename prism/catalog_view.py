"""The catalog as the report map page shows it: items, what they read, and the findings between them."""
from analysis import catalog

from . import registry

KEY_KINDS = {"same_file": "同一份檔案", "relationship": "跨系統的關聯", "orphans": "關聯對不到的值",
             "overlap": "不同報表的值重疊"}


def _title(r: catalog.Report) -> str:
    meta = registry.load_meta(r.name) or registry.EXAMPLES.get(r.name, {})
    return meta.get("title") or r.title


def _kind(r: catalog.Report) -> str:
    meta = registry.load_meta(r.name)
    return meta.get("kind") or ("workbook" if not r.model.measures else "report")


def view(cat: catalog.Catalog) -> dict:
    items, sources, links = [], {}, []
    for r in cat.reports:
        reads = sorted({catalog.source_id(t) for t in r.model.tables.values() if catalog.source_id(t)})
        items.append({"name": r.name, "title": _title(r), "kind": _kind(r), "tables": len(r.model.tables),
                      "measures": len(r.model.measures), "reads": reads,
                      "emphasis": [f"{e.page}／{e.visual}：{e.text}" for e in r.emphasis
                                   if e.explicit and e.kind not in ("bookmarks", "hidden_page")][:20]})
        for sid in reads:
            sources.setdefault(sid, {"id": sid, "label": catalog.source_label(sid), "used_by": []})
            sources[sid]["used_by"].append(r.name)
    # the same databases / files, for the graph: one node per system with its tables
    systems = {}
    for sid, src in sources.items():
        if len(set(src["used_by"])) < 2:
            continue
        key = catalog.system_of(sid)
        label = src["label"].rsplit(".", 1)[0] if sid.startswith(("oracle:", "sql:")) else src["label"].split("／")[0]
        sy = systems.setdefault(key, {"id": key, "label": label, "tables": [], "used_by": []})
        sy["tables"].append(src["label"])
        sy["used_by"] = sorted(set(sy["used_by"]) | set(src["used_by"]))
    titles = {i["name"]: i["title"] for i in items}

    def who(cs):
        return [{"report": c.report, "title": titles.get(c.report, c.report), "measure": c.measure} for c in cs]

    synonyms = [{"members": who(g), "formula": g[0].readable(), "renamed": len({c.measure for c in g}) > 1}
                for g in cat.same if len({c.report for c in g}) > 1]
    conflicts = [{"name": cf.name, "same_system": cf.same_system,
                  "groups": [{"members": who(g), "formula": g[0].readable()} for g in cf.groups]}
                 for cf in cat.conflicts]
    def pair(k):
        return frozenset((k.left[0].split(":", 1)[0], k.right[0].split(":", 1)[0]))

    # an Excel that is a copy of the report's source overlaps it everywhere; that is not news
    copies = {pair(k) for k in cat.keys if k.kind == "same_file"}
    keys = []
    for k in cat.keys:
        if k.kind == "overlap" and pair(k) in copies:
            continue
        near_l, near_r = {a for a, _ in k.near}, {b for _, b in k.near}
        keys.append({"kind": k.kind, "kind_text": KEY_KINDS.get(k.kind, k.kind), "left": k.left, "right": k.right,
                     "left_source": catalog.short(k.left_source), "right_source": catalog.short(k.right_source),
                     "shared": k.shared, "left_count": k.left_count, "right_count": k.right_count,
                     "left_only": [v for v in k.left_only if v not in near_l][:30], "left_only_n": len(k.left_only),
                     "right_only": [v for v in k.right_only if v not in near_r][:30] if k.kind != "orphans" else [],
                     "right_only_n": len(k.right_only) if k.kind != "orphans" else 0,
                     "near": [list(x) for x in k.near[:20]], "note": k.note,
                     # values two reports happen to share are a possible join, not a mismatch to fix
                     "ok": None if k.kind == "overlap" else
                     not (k.left_only or (k.right_only and k.kind != "orphans") or k.near)})
        a, b = k.left[0].split(":", 1)[0], k.right[0].split(":", 1)[0]
        if a != b:
            links.append({"from": a, "to": b, "kind": k.kind, "ok": keys[-1]["ok"]})
    for s in synonyms:
        reps = list(dict.fromkeys(m["report"] for m in s["members"]))
        links += [{"from": reps[0], "to": x, "kind": "synonym", "ok": True} for x in reps[1:]]
    for cf in conflicts:
        reps = [g["members"][0]["report"] for g in cf["groups"]]
        if cf["same_system"]:
            links += [{"from": reps[0], "to": x, "kind": "conflict", "ok": False} for x in reps[1:]]
    return {"items": items, "sources": list(sources.values()), "systems": list(systems.values()),
            "synonyms": synonyms, "conflicts": conflicts,
            "keys": keys, "links": links, "unexpanded": [{"report": c.report, "measure": c.measure, "reason": c.reason}
                                                       for c in cat.measures.values() if not c.tree],
            "notes": cat.notes}
