"""Generate one business day of FACT_SALES rows."""
import bisect
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config as cfg
from . import dimensions as dims
from .scenarios import Scenario

QUANTITY_DIST = {
    "飲料": ([1, 2, 3, 4, 6], [0.45, 0.25, 0.12, 0.10, 0.08]),
    "零食": ([1, 2, 3, 4, 6], [0.45, 0.25, 0.12, 0.10, 0.08]),
    "生鮮食品": ([1, 2, 3], [0.65, 0.25, 0.10]),
    "日用品": ([1, 2, 3], [0.65, 0.25, 0.10]),
    "美妝保養": ([1, 2], [0.88, 0.12]),
    "3C配件": ([1, 2], [0.88, 0.12]),
}


@dataclass
class DayResult:
    rows: list[tuple]
    etl_status: str = "SUCCESS"
    etl_message: str | None = None
    scenario_ids: list[str] = field(default_factory=list)


class Generator:
    def __init__(self):
        self.stores = dims.stores()
        self.channels = dims.channels()
        self.products_by_cat = defaultdict(list)
        for p in dims.products():
            self.products_by_cat[p.category].append(p)
        self.categories = list(self.products_by_cat)
        # members per (region, segment), sorted by join date for "joined before d" lookups
        self.members = defaultdict(list)
        self.guest = {}
        for c in dims.customers():
            if c.segment == "非會員":
                self.guest[c.region_code] = c
            else:
                self.members[(c.region_code, c.segment)].append(c)
        self.member_join = {}
        for key, lst in self.members.items():
            lst.sort(key=lambda c: c.join_date)
            self.member_join[key] = [c.join_date for c in lst]

    # ---------- hidden calendar effects ----------
    @staticmethod
    def calendar_effects(d: date):
        traffic = {"實體": 1.0, "線上": 1.0}
        cat_mult: dict[str, float] = {}
        promo_share = cfg.BASE_PROMO_LINE_SHARE
        holiday = cfg.HOLIDAYS.get(d)
        eve = next((h for h, n in cfg.HOLIDAYS.items() if n == "除夕" and h.year == d.year), None)
        if eve:
            if eve - timedelta(days=10) <= d < eve:
                traffic["實體"] *= 1.25
                cat_mult.update({"零食": 1.35, "飲料": 1.2, "生鮮食品": 1.2})
            elif eve <= d <= eve + timedelta(days=3):
                traffic["實體"] *= 0.55
                traffic["線上"] *= 0.80
        if (d.month, d.day) == (11, 11):
            traffic["線上"] *= 2.6
            promo_share = 0.45
        elif d.month == 11 and 8 <= d.day <= 10:
            traffic["線上"] *= 1.3
        elif (d.month, d.day) == (12, 12):
            traffic["線上"] *= 1.8
            promo_share = 0.35
        mid_autumn = next((h for h, n in cfg.HOLIDAYS.items() if n == "中秋節" and h.year == d.year), None)
        if mid_autumn and mid_autumn - timedelta(days=4) <= d <= mid_autumn:
            cat_mult.update({"生鮮食品": 1.6, "零食": 1.3, "飲料": 1.2})
        if date(d.year, 10, 15) <= d <= date(d.year, 11, 5):  # anniversary sale
            traffic["實體"] *= 1.12
            promo_share = max(promo_share, 0.35)
        if holiday and holiday not in ("除夕", "春節", "雙11", "雙12"):
            traffic["實體"] *= 1.15
            traffic["線上"] *= 0.95
        return traffic, cat_mult, promo_share

    # ---------- helpers ----------
    @staticmethod
    def copies(rng: random.Random, multiplier: float) -> int:
        whole = math.floor(multiplier)
        return whole + (1 if rng.random() < multiplier - whole else 0)

    def pick_customer(self, rng, region_code: str, segment: str, d: date):
        if segment == "非會員":
            return self.guest[region_code]
        key = (region_code, segment)
        n = bisect.bisect_right(self.member_join[key], d)
        return self.members[key][rng.randrange(n)] if n else self.guest[region_code]

    # ---------- main ----------
    def generate(self, d: date, scenarios: list[Scenario]) -> DayResult:
        rng = random.Random(f"{cfg.SEED}-{d.isoformat()}")
        date_key = int(d.strftime("%Y%m%d"))
        traffic_cal, cat_cal, promo_share = self.calendar_effects(d)
        order_scen = [s for s in scenarios if not s.is_line_level]
        line_scen = [s for s in scenarios if s.is_line_level]
        years = (d - cfg.HISTORY_START).days / 365.25
        seg_names = list(cfg.SEGMENTS)
        rows, seq = [], 0

        for store in self.stores:
            if d < store.open_date:
                continue
            ramp = min(1.0, ((d - store.open_date).days + 1) / cfg.NEW_STORE_RAMP_DAYS)
            for ch in self.channels:
                online = ch.type == "線上"
                mean = (store.base_orders * ch.traffic_ratio * ramp
                        * (cfg.REGIONS[store.region_code][1] if online else 1.0)
                        * cfg.WEEKDAY_FACTOR[ch.type][d.weekday()]
                        * cfg.MONTH_FACTOR[d.month - 1]
                        * (1 + cfg.ANNUAL_GROWTH[ch.type]) ** years
                        * traffic_cal[ch.type]
                        * math.exp(rng.gauss(0, cfg.DAILY_NOISE_SIGMA) - cfg.DAILY_NOISE_SIGMA ** 2 / 2))
                n_orders = max(0, round(rng.gauss(mean, math.sqrt(mean)))) if mean > 0 else 0

                cat_weights = []
                for cat in self.categories:
                    w = cfg.CATEGORY_WEIGHT_BY_CHANNEL[ch.code][cat]
                    w *= cfg.CATEGORY_REGION_ADJ.get(store.region_code, {}).get(cat, 1.0)
                    w *= cat_cal.get(cat, 1.0)
                    cat_weights.append(w)

                for _ in range(n_orders):
                    seg_weights = [cfg.SEGMENTS[s][0] for s in seg_names]
                    if online:
                        seg_weights[0] = 0.0  # online orders always belong to a member
                    segment = rng.choices(seg_names, seg_weights)[0]
                    order_attrs = dict(region=store.region_name, store=store.code,
                                       channel=ch.name, segment=segment)
                    matched = [s for s in order_scen if s.matches(**order_attrs)]
                    volume = math.prod(s.effect("volume") for s in matched)
                    missing = 1 - math.prod(1 - s.effect("missing_rate") for s in matched)
                    for _copy in range(self.copies(rng, volume)):
                        if missing and rng.random() < missing:
                            continue
                        seq += 1
                        order_rows = self._order_lines(
                            rng, d, date_key * 100000 + seq, date_key, store, ch, segment,
                            order_attrs, cat_weights, promo_share, matched, line_scen)
                        rows.extend(order_rows)

        result = DayResult(rows, scenario_ids=[s.id for s in scenarios])
        flagged = [s for s in scenarios if s.etl_status]
        if flagged:
            result.etl_status = flagged[0].etl_status
            result.etl_message = f"{flagged[0].etl_status} load"
        return result

    def _order_lines(self, rng, d, order_id, date_key, store, ch, segment, order_attrs,
                     cat_weights, promo_share, order_matched, line_scen):
        customer = self.pick_customer(rng, store.region_code, segment, d)
        n_lines = rng.choices([1, 2, 3, 4], cfg.LINES_PER_ORDER[ch.type])[0]
        if rng.random() < cfg.SEGMENTS[segment][1]:
            n_lines += 1
        used, rows = set(), []
        for _ in range(n_lines):
            cat = rng.choices(self.categories, cat_weights)[0]
            prods = self.products_by_cat[cat]
            prod = rng.choices(prods, [p.popularity for p in prods])[0]
            if prod.id in used:
                continue
            used.add(prod.id)
            matched = order_matched + [s for s in line_scen
                                       if s.matches(**order_attrs, category=cat, product=prod.code)]
            line_only = [s for s in matched if s.is_line_level]
            n_units = self.copies(rng, math.prod(s.effect("volume") for s in line_only))
            if n_units == 0:
                continue
            if any(rng.random() < s.effect("missing_rate") for s in line_only):
                continue
            values, weights = QUANTITY_DIST[cat]
            qty = sum(rng.choices(values, weights)[0] for _ in range(n_units))
            unit_price = round(prod.list_price * math.prod(s.effect("price") for s in matched))
            rate = rng.uniform(*cfg.BASE_PROMO_RATE_RANGE) if rng.random() < promo_share else 0.0
            rate = min(0.6, rate + sum(s.effect("extra_discount") for s in matched))
            discount = round(unit_price * qty * rate)
            cost = round(prod.std_cost * math.prod(s.effect("cost") for s in matched) * qty, 2)
            rows.append((order_id, len(rows) + 1, date_key, store.id, ch.id, prod.id, customer.id,
                         qty, unit_price, discount, unit_price * qty - discount, cost))
        return rows
