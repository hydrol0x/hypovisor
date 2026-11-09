"""Command line: `hypovisor init-db | import clarity|tandem PATH | meals-csv PATH | synth DIR | recompute | serve`."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from .config import settings
from .db import init_db, session_scope


def cmd_init_db(_):
    init_db()
    print(f"initialized {settings.database_url}")


def cmd_import(args):
    init_db()
    from .analytics.meal_response import recompute_all
    from .importers import import_clarity_csv, import_tandem_source

    with session_scope() as s:
        if args.kind == "clarity":
            r = import_clarity_csv(s, args.path)
        elif args.kind == "nightscout-json":
            from .importers.nightscout import import_nightscout_files

            p = Path(args.path)
            r = import_nightscout_files(s, sorted(p.glob("*.json*")) if p.is_dir() else [p])
        else:
            r = import_tandem_source(s, args.path)
        n = recompute_all(s)
    d = r.as_dict()
    print(
        f"{r.source}: +{d['glucose_added']} glucose, +{d['bolus_added']} boluses, +{d['basal_added']} basal, +{d['events_added']} events; "
        f"skipped {d['glucose_skipped'] + d['bolus_skipped'] + d['basal_skipped']} duplicates; range {d['first_ts']} .. {d['last_ts']}; recomputed {n} meals"
    )
    for w in r.warnings:
        print("warning:", w)


def cmd_meals_csv(args):
    """Bulk-load meals from a CSV with columns local_ts, description, carbs_g[, fat_g, protein_g, fiber_g]."""
    init_db()
    from .analytics.meal_response import compute_response
    from .food.knowledge_base import learn_from_item
    from .models import Meal, MealItem
    from .timeutil import local_naive_to_utc

    n = 0
    with session_scope() as s, open(args.path, newline="") as fh:
        for row in csv.DictReader(fh):
            ts, off = local_naive_to_utc(datetime.fromisoformat(row["local_ts"]))
            f = lambda k: float(row[k]) if row.get(k) not in (None, "") else None  # noqa: E731
            meal = Meal(
                ts=ts,
                utc_offset_min=off,
                description=row["description"],
                status="confirmed",
                values_source="user",
                carbs_g=f("carbs_g"),
                carbs_low_g=f("carbs_g"),
                carbs_high_g=f("carbs_g"),
                fat_g=f("fat_g"),
                protein_g=f("protein_g"),
                fiber_g=f("fiber_g"),
            )
            item = MealItem(
                name=row["description"],
                carbs_g=f("carbs_g"),
                fat_g=f("fat_g"),
                protein_g=f("protein_g"),
                fiber_g=f("fiber_g"),
                values_source="user",
                confidence=1.0,
            )
            meal.items.append(item)
            s.add(meal)
            s.flush()
            learn_from_item(s, item, user_corrected=True)
            meal.response = compute_response(s, meal)
            n += 1
    print(f"loaded {n} meals")


def cmd_synth(args):
    from .synth import generate

    info = generate(Path(args.dir), days=args.days, seed=args.seed)
    print(info)
    if args.load:
        init_db()
        from .analytics.meal_response import recompute_all
        from .importers import import_clarity_csv, import_tandem_source

        with session_scope() as s:
            import_clarity_csv(s, info["clarity"])
            import_tandem_source(s, info["tandem"])
        cmd_meals_csv(argparse.Namespace(path=info["meals"]))
        with session_scope() as s:
            recompute_all(s)
        print("loaded synthetic data into", settings.database_url)


def cmd_recompute(_):
    init_db()
    from .analytics.meal_response import recompute_all

    with session_scope() as s:
        print("recomputed", recompute_all(s), "meal responses")


def cmd_forecast(args):
    init_db()
    from .forecast.model import GlucoseForecaster, evaluate_holdout

    with session_scope() as s:
        if args.action == "train":
            m = GlucoseForecaster()
            meta = m.fit_from_db(s, days=args.days)
            print("saved", m.save())
            print(meta)
        elif args.action == "evaluate":
            print(evaluate_holdout(s, days=args.days))
        else:
            m = GlucoseForecaster.load()
            print(m.predict_latest(s) if m else "no trained model; run: hypovisor forecast train")


def cmd_serve(args):
    import uvicorn

    init_db()
    uvicorn.run("hypovisor.api.app:app", host=args.host, port=args.port, reload=args.reload)


def main(argv=None):
    p = argparse.ArgumentParser(prog="hypovisor")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    ip = sub.add_parser("import", help="import a vendor export")
    ip.add_argument("kind", choices=["clarity", "tandem", "nightscout-json"])
    ip.add_argument("path")
    ip.set_defaults(fn=cmd_import)
    mp = sub.add_parser("meals-csv", help="bulk load meals from CSV")
    mp.add_argument("path")
    mp.set_defaults(fn=cmd_meals_csv)
    sp = sub.add_parser("synth", help="generate synthetic Clarity + Tandem exports")
    sp.add_argument("dir")
    sp.add_argument("--days", type=int, default=21)
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--load", action="store_true", help="also import into the configured database")
    sp.set_defaults(fn=cmd_synth)
    sub.add_parser("recompute").set_defaults(fn=cmd_recompute)
    fp = sub.add_parser("forecast", help="train / evaluate / predict the 30-60 min glucose forecaster")
    fp.add_argument("action", choices=["train", "evaluate", "predict"])
    fp.add_argument("--days", type=int, default=120)
    fp.set_defaults(fn=cmd_forecast)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(fn=cmd_serve)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
