"""Scenario injection: known business events used as ground truth for evaluating analysis.

The scenario file lives outside Oracle on purpose, so the analysis layer cannot read the answers.
"""
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .db import ROOT

DEFAULT_FILE = ROOT / "scenarios" / "scenarios.yaml"
FILTER_KEYS = {"region", "store", "channel", "category", "product", "segment"}
LINE_LEVEL_KEYS = {"category", "product"}
EFFECT_DEFAULTS = {"volume": 1.0, "price": 1.0, "cost": 1.0, "extra_discount": 0.0, "missing_rate": 0.0}


@dataclass
class Scenario:
    id: str
    description: str
    start: date
    end: date | None
    ramp_days: int
    filter: dict[str, set[str]]
    effects: dict[str, float]
    etl_status: str | None = None
    weight: float = field(default=1.0)  # ramp progress for the current date, 0..1

    def active_on(self, d: date) -> bool:
        return self.start <= d and (self.end is None or d <= self.end)

    def at(self, d: date) -> "Scenario":
        progress = 1.0 if self.ramp_days <= 1 else min(1.0, ((d - self.start).days + 1) / self.ramp_days)
        return Scenario(self.id, self.description, self.start, self.end, self.ramp_days,
                        self.filter, self.effects, self.etl_status, progress)

    def effect(self, name: str) -> float:
        """Effect value scaled by ramp progress (multiplicative effects ramp from 1, additive from 0)."""
        base = EFFECT_DEFAULTS[name]
        return base + (self.effects.get(name, base) - base) * self.weight

    @property
    def is_line_level(self) -> bool:
        return bool(LINE_LEVEL_KEYS & self.filter.keys())

    def matches(self, **attrs: str) -> bool:
        """attrs not supplied are treated as 'unknown yet' and do not block the match."""
        return all(key not in attrs or attrs[key] in allowed for key, allowed in self.filter.items())


def load(path: Path = DEFAULT_FILE) -> list[Scenario]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for item in raw.get("scenarios", []):
        if not item.get("enabled", True):
            continue
        flt = {k: set(v if isinstance(v, list) else [v]) for k, v in (item.get("filter") or {}).items()}
        unknown = flt.keys() - FILTER_KEYS
        if unknown:
            raise ValueError(f"scenario {item['id']}: unknown filter keys {unknown}")
        effects = item.get("effects") or {}
        bad = effects.keys() - EFFECT_DEFAULTS.keys()
        if bad:
            raise ValueError(f"scenario {item['id']}: unknown effects {bad}")
        out.append(Scenario(
            id=item["id"], description=item.get("description", ""),
            start=item["start"], end=item.get("end"), ramp_days=int(item.get("ramp_days", 1)),
            filter=flt, effects={k: float(v) for k, v in effects.items()},
            etl_status=item.get("etl_status"),
        ))
    return out


def active(scenarios: list[Scenario], d: date) -> list[Scenario]:
    return [s.at(d) for s in scenarios if s.active_on(d)]
