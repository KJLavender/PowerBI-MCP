"""Daily change analysis on top of the Power BI semantic model.

  python -m analysis snapshot [--start D] [--end D]   Oracle → DuckDB daily aggregates
  python -m analysis report [--date D]                write reports/<date>.md and data/reports/<date>.json
  python -m analysis daily                            snapshot (last 3 days + new) and report for yesterday
  python -m analysis evaluate [--start D] [--end D]   score against scenarios/scenarios.yaml
  python -m analysis model                            show what was read from the Power BI project
"""
import argparse
from datetime import date, timedelta

from simulator import config as sim_cfg

from . import context, evaluate, report, store
from .semantic import ROOT


def snapshot_range(start: date | None, end: date | None) -> tuple[date, date]:
    end = end or date.today() - timedelta(days=1)
    if start is None:
        with store.open_db() as db:
            last = store.last_snapshot_date(db)
        # re-take the last 3 days: late-arriving rows and corrections are picked up
        start = last - timedelta(days=3) if last else sim_cfg.HISTORY_START
    return start, end


def cmd_snapshot(args) -> None:
    ctx = context.load()
    d0, d1 = snapshot_range(args.start, args.end)
    store.snapshot(ctx, d0, d1)


def cmd_report(args) -> None:
    ctx = context.load()
    if args.date is None:
        with store.open_db(read_only=True) as db:
            args.date = store.last_snapshot_date(db)
    print(f"report written: {report.write(ctx, args.date)}")


def cmd_daily(args) -> None:
    ctx = context.load()
    d0, d1 = snapshot_range(None, None)
    store.snapshot(ctx, d0, d1, verbose=False)
    print(f"snapshot {d0} .. {d1}")
    print(f"report written: {report.write(ctx, d1)}")


def cmd_evaluate(args) -> None:
    r = evaluate.run(args.start, args.end)
    text = evaluate.render(r)
    out = ROOT / "reports" / "evaluation.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(text)


def cmd_model(_args) -> None:
    ctx = context.load()
    print("Relationships:")
    for r in ctx.model.relationships:
        print(f"  {r}{'' if r.active else ' (inactive)'}")
    print("\nDimensions (config → model):")
    for d in ctx.dims.values():
        print(f"  {d.key:<9}{d.label:<6} {d.table}[{d.column}]  via {' ; '.join(map(str, d.path))}")
    print("\nMetrics (DAX → formula over fact aggregates):")
    for m in ctx.metrics:
        print(f"  {m:<8} {ctx.formulas[m].expr}")
    for m, why in ctx.skipped.items():
        print(f"  (skipped) {m}: {why}")
    print("\nReport usage:")
    for u in ctx.model.usage:
        if u.measures:
            print(f"  {u.page} / {u.title or u.visual}: {', '.join(u.measures)}")


def main() -> None:
    p = argparse.ArgumentParser(prog="analysis", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--start", type=date.fromisoformat)
    s.add_argument("--end", type=date.fromisoformat)
    s.set_defaults(func=cmd_snapshot)
    s = sub.add_parser("report")
    s.add_argument("--date", type=date.fromisoformat)
    s.set_defaults(func=cmd_report)
    sub.add_parser("daily").set_defaults(func=cmd_daily)
    s = sub.add_parser("evaluate")
    s.add_argument("--start", type=date.fromisoformat, default=date(2026, 5, 15))
    s.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    s.set_defaults(func=cmd_evaluate)
    sub.add_parser("model").set_defaults(func=cmd_model)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
