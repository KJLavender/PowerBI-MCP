"""Daily change analysis on top of the Power BI semantic model.

  python -m analysis snapshot [--start D] [--end D]   source (Oracle / Excel) → DuckDB daily aggregates
  python -m analysis report [--date D]                write reports/<date>.md and data/reports/<date>.json
  python -m analysis daily                            snapshot (last 3 days + new) and report for yesterday
  python -m analysis evaluate [--start D] [--end D]   score against scenarios/scenarios.yaml
  python -m analysis model                            show what was read from the Power BI project
  python -m analysis reconcile [--port P]             compare with the numbers Power BI Desktop computes (DAX)

The report analysed is chosen with PBI_DATASET (default: retail; e.g. PBI_DATASET=superstore).
"""
import argparse
from datetime import date, timedelta

from . import context, dataset, evaluate, report, sources, store


def snapshot_range(ctx, start: date | None, end: date | None) -> tuple[date, date]:
    first, last_available = sources.date_range(ctx)
    end = end or last_available
    if start is None:
        with store.open_db() as db:
            last = store.last_snapshot_date(db)
        # re-take the last 3 days: late-arriving rows and corrections are picked up
        start = max(last - timedelta(days=3), first) if last else first
    return start, end


def cmd_snapshot(args) -> None:
    ctx = context.load()
    d0, d1 = snapshot_range(ctx, args.start, args.end)
    store.snapshot(ctx, d0, d1)


def cmd_report(args) -> None:
    ctx = context.load()
    if args.date is None:
        with store.open_db(read_only=True) as db:
            args.date = store.last_snapshot_date(db)
    print(f"report written: {report.write(ctx, args.date)}")


def cmd_daily(args) -> None:
    ctx = context.load()
    d0, d1 = snapshot_range(ctx, None, None)
    store.snapshot(ctx, d0, d1, verbose=False)
    print(f"snapshot {d0} .. {d1}")
    print(f"report written: {report.write(ctx, d1)}")


def cmd_evaluate(args) -> None:
    span = context.load().config.get("evaluation", {})
    start = args.start or span.get("start") or date(2026, 5, 15)
    end = args.end or span.get("end") or date.today() - timedelta(days=1)
    r = evaluate.run(start, end)
    text = evaluate.render(r)
    out = dataset.REPORT_DIR / "evaluation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
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
    s.add_argument("--start", type=date.fromisoformat)
    s.add_argument("--end", type=date.fromisoformat)
    s.set_defaults(func=cmd_evaluate)
    sub.add_parser("model").set_defaults(func=cmd_model)
    s = sub.add_parser("reconcile")
    s.add_argument("--port", type=int)
    s.add_argument("--samples", type=int, default=6)
    s.set_defaults(func=lambda a: __import__("analysis.reconcile", fromlist=["main"]).main(a.port, a.samples))
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
