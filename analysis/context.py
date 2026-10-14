"""Analysis context: semantic model + business config, validated against each other."""
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import yaml

from . import dataset, semantic
from .semantic import Base, Formula, Relationship, SemanticModel

DATA_DIR = dataset.DATA_DIR

# Measures with a known business role. Analyses that need e.g. "the volume metric" look them up here;
# a dataset config can rename them (roles: {margin_rate: 利潤率}). Missing ones are simply skipped.
DEFAULT_ROLES = {"sales": "銷售額", "volume": "銷售數量", "orders": "訂單數", "unit_price": "平均成交單價",
                 "discount_rate": "折扣率", "margin_rate": "毛利率", "extra": ["成本", "毛利", "折扣金額"]}


@dataclass
class Dimension:
    key: str
    label: str
    table: str
    column: str          # model column name
    source_column: str   # physical column
    parent: str | None
    path: list[Relationship]
    scan: bool = True    # False: kept for drill-down/price analysis only, not scanned for findings


@dataclass
class Context:
    model: SemanticModel
    config: dict
    fact: str
    dims: dict[str, Dimension]
    formulas: dict[str, Formula]
    bases: dict[str, Base]
    skipped: dict[str, str]
    date_dim: Dimension
    holiday_source: str | None
    warnings: list[str] = field(default_factory=list)
    report_filters: list[tuple] = field(default_factory=list)  # report-level filters applied to every snapshot

    @cached_property
    def roles(self) -> dict:
        return {**DEFAULT_ROLES, **self.config.get("roles", {})}

    def role(self, name: str) -> str | None:
        """Measure name for a business role, if the model has it."""
        m = self.roles.get(name)
        return m if m in self.formulas else None

    @property
    def dq(self) -> dict:
        return self.config.get("data_quality") or {}

    @cached_property
    def source(self) -> dict:
        """Where snapshots read from: the config's `source` (e.g. the Power BI engine), else the model's partitions."""
        return self.config.get("source") or self.model.tables[self.fact].source or {"kind": "oracle"}

    @cached_property
    def metrics(self) -> list[str]:
        """Translatable measures, report-used ones first."""
        used = [m for m in self.model.used_measures() if m in self.formulas]
        return used + [m for m in self.formulas if m not in used]

    def describe(self, measure: str) -> str:
        return self.model.measures[measure].description

    def fmt(self, measure: str, value: float | None) -> str:
        if value is None or value != value:
            return "—"
        f = self.model.measures[measure].format_string
        if "%" in f:
            return f"{value:.1%}"
        if ".0" in f:
            return f"{value:,.1f}"
        return f"{value:,.0f}"

    def drivers(self, measure: str) -> list[tuple[str, str]]:
        """Multiplicative drivers from the model: R = DIVIDE([X], [Y])  ⇒  X = Y × R."""
        norm = lambda s: re.sub(r"[()\s]", "", s)  # noqa: E731
        target = norm(self.formulas[measure].expr)
        out = []
        for name, f in self.formulas.items():
            if f.is_ratio and f.numerator and f.denominator and norm(f.numerator) == target:
                y = next((m for m, g in self.formulas.items() if norm(g.expr) == norm(f.denominator)), None)
                if y:
                    out.append((y, name))
        return out


def _resolve(model: SemanticModel, fact: str, ref: str) -> tuple[str, str, str, list[Relationship]]:
    table, column = ref.split(".", 1)
    if table not in model.tables or column not in model.tables[table].columns:
        raise ValueError(f"config column {ref} does not exist in the Power BI model")
    path = model.join_path(fact, table)
    return table, column, model.tables[table].columns[column].source_column, path


def load(config_path: Path = dataset.CONFIG) -> Context:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    model = semantic.load()
    fact = config["fact"]
    dims = {}
    for key, spec in config["dimensions"].items():
        table, column, source, path = _resolve(model, fact, spec["column"])
        dims[key] = Dimension(key, spec["label"], table, column, source, spec.get("parent"), path,
                              spec.get("scan", True))
    for combo in config.get("combinations", []):
        missing = set(combo) - dims.keys()
        if missing:
            raise ValueError(f"combination {combo} uses unknown dimensions {missing}")
    table, column, source, path = _resolve(model, fact, config["date"])
    date_dim = Dimension("business_date", "日期", table, column, source, None, path)
    holiday_source = _resolve(model, fact, config["holiday"])[2] if config.get("holiday") else None
    formulas, bases, skipped = semantic.translate_all(model, fact)
    bases.setdefault("rows", Base("rows", "COUNT(*)", dax=f"COUNTROWS({semantic.dax_table(fact)})"))
    ctx = Context(model, config, fact, dims, formulas, bases, skipped, date_dim, holiday_source)
    for c in model.report_filters:
        try:
            if c[0] != fact:
                model.join_path(fact, c[0])
            ctx.report_filters.append(c)
        except ValueError:
            ctx.warnings.append(f"報表篩選 {semantic.describe_filter(c)} 的資料表與事實表沒有多對一關聯，無法套用")
    ctx.warnings += [f"報表篩選無法解讀：{n}" for n in model.filter_notes]
    return ctx
