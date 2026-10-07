"""Build dimension rows (deterministic for a given SEED)."""
import random
from dataclasses import dataclass
from datetime import date, timedelta

from . import config as cfg

WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
AGE_GROUPS = [("18-24", 0.14), ("25-34", 0.28), ("35-44", 0.26), ("45-54", 0.18), ("55+", 0.14)]


@dataclass(frozen=True)
class Store:
    id: int
    code: str
    name: str
    city: str
    region_code: str
    region_name: str
    type: str
    area: int
    open_date: date
    base_orders: float


@dataclass(frozen=True)
class Channel:
    id: int
    code: str
    name: str
    type: str
    traffic_ratio: float


@dataclass(frozen=True)
class Product:
    id: int
    code: str
    name: str
    category: str
    brand: str
    list_price: float
    std_cost: float
    popularity: float


@dataclass(frozen=True)
class Customer:
    id: int
    code: str
    segment: str
    gender: str
    age_group: str
    region_code: str
    join_date: date


def date_rows():
    d = cfg.CALENDAR_START
    while d <= cfg.CALENDAR_END:
        yield (
            int(d.strftime("%Y%m%d")), d, d.year, (d.month - 1) // 3 + 1, d.month,
            d.strftime("%Y-%m"), d.isocalendar()[1], d.isoweekday(),
            WEEKDAY_NAMES[d.weekday()], "Y" if d.weekday() >= 5 else "N",
            cfg.HOLIDAYS.get(d),
        )
        d += timedelta(days=1)


def stores() -> list[Store]:
    return [
        Store(i, code, name, city, rc, cfg.REGIONS[rc][0], typ, area, opened, base)
        for i, (code, name, city, rc, typ, area, opened, base) in enumerate(cfg.STORES, start=1)
    ]


def channels() -> list[Channel]:
    return [Channel(*c) for c in cfg.CHANNELS]


def products() -> list[Product]:
    out, pid = [], 1
    for category, items in cfg.PRODUCTS.items():
        for name, brand, price, cost, pop in items:
            out.append(Product(pid, f"P{pid:04d}", name, category, brand, price, cost, pop))
            pid += 1
    return out


def customers() -> list[Customer]:
    """One anonymous '非會員' customer per region, then MEMBER_COUNT members."""
    rng = random.Random(f"{cfg.SEED}-customers")
    out = []
    for i, rc in enumerate(cfg.REGIONS, start=1):
        out.append(Customer(i, f"GUEST-{rc}", "非會員", "未知", "未知", rc, date(2018, 1, 1)))
    region_codes = list(cfg.REGIONS)
    region_weights = [sum(s.base_orders for s in stores() if s.region_code == rc) for rc in region_codes]
    span = (date(2026, 9, 30) - date(2019, 1, 1)).days
    for n in range(cfg.MEMBER_COUNT):
        cid = len(cfg.REGIONS) + n + 1
        # membership grows over time: skew join dates towards recent years
        join = date(2019, 1, 1) + timedelta(days=int(span * rng.random() ** 0.7))
        out.append(Customer(
            cid, f"M{cid:07d}",
            "VIP" if rng.random() < cfg.VIP_SHARE_OF_MEMBERS else "一般會員",
            rng.choice(["女", "男"]) if rng.random() < 0.97 else "未提供",
            rng.choices([a for a, _ in AGE_GROUPS], [w for _, w in AGE_GROUPS])[0],
            rng.choices(region_codes, region_weights)[0],
            join,
        ))
    return out


def load(cur) -> None:
    cur.executemany(
        "INSERT INTO DIM_DATE VALUES (:1,:2,:3,:4,:5,:6,:7,:8,:9,:10,:11)", list(date_rows()))
    cur.executemany(
        "INSERT INTO DIM_STORE VALUES (:1,:2,:3,:4,:5,:6,:7,:8,:9)",
        [(s.id, s.code, s.name, s.city, s.region_code, s.region_name, s.type, s.area, s.open_date)
         for s in stores()])
    cur.executemany(
        "INSERT INTO DIM_CHANNEL VALUES (:1,:2,:3,:4)",
        [(c.id, c.code, c.name, c.type) for c in channels()])
    cur.executemany(
        "INSERT INTO DIM_PRODUCT VALUES (:1,:2,:3,:4,:5,:6,:7)",
        [(p.id, p.code, p.name, p.category, p.brand, p.list_price, p.std_cost) for p in products()])
    cur.executemany(
        "INSERT INTO DIM_CUSTOMER VALUES (:1,:2,:3,:4,:5,:6,:7)",
        [(c.id, c.code, c.segment, c.gender, c.age_group, cfg.REGIONS[c.region_code][0], c.join_date)
         for c in customers()])
