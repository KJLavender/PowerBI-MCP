"""Analysis context: semantic model + business config, validated against each other."""
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import yaml

from . import semantic
from .semantic import Base, Formula, Relationship, SemanticModel

HERE = Path(__file__).resolve().parent
DATA_DIR = semantic.ROOT / "data"


@dataclass
class Dimension:
    key: str
    label: str
    table: str
    column: str          # model column name
    source_column: str   # physical column
    parent: str | None
    path: list[Relationship]


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
    holiday_source: str
    warnings: list[str] = field(default_factory=list)

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


def load(config_path: Path = HERE / "config.yaml") -> Context:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    model = semantic.load()
    fact = config["fact"]
    dims = {}
    for key, spec in config["dimensions"].items():
        table, column, source, path = _resolve(model, fact, spec["column"])
        dims[key] = Dimension(key, spec["label"], table, column, source, spec.get("parent"), path)
    for combo in config.get("combinations", []):
        missing = set(combo) - dims.keys()
        if missing:
            raise ValueError(f"combination {combo} uses unknown dimensions {missing}")
    table, column, source, path = _resolve(model, fact, config["date"])
    date_dim = Dimension("business_date", "日期", table, column, source, None, path)
    _, _, holiday_source, _ = _resolve(model, fact, config["holiday"])
    formulas, bases, skipped = semantic.translate_all(model, fact)
    bases.setdefault("rows", Base("rows", "COUNT(*)"))
    return Context(model, config, fact, dims, formulas, bases, skipped, date_dim, holiday_source)
