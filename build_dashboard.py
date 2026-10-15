"""
build_dashboard.py
==================
OpenCode/standalone builder for the AAFL 24-Month MRP dashboard.

Pulls live data from the DWH through the enterprise-api-gateway (aafl_gateway),
merges it with the supplied static dataset (aafl_static_data.json), and writes a
single self-contained HTML dashboard named ``index.html`` into this folder.

Usage:
    python build_dashboard.py                 # full build incl. live market intel
    python build_dashboard.py --no-market     # skip yfinance/RSS (offline-safe)
    python build_dashboard.py --out out.html  # custom output path
"""
import argparse
import datetime
import json
import os
import sys
import time

import aafl_mrp

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the AAFL MRP dashboard (index.html).")
    ap.add_argument("--out", default=os.path.join(HERE, "index.html"))
    ap.add_argument("--no-market", action="store_true", help="skip live market intel")
    ap.add_argument("--snapshot", default=os.path.join(HERE, "aafl_live_snapshot.json"))
    args = ap.parse_args(argv)

    t0 = time.time()
    print("=" * 62)
    print(" AAFL 24-Month MRP - build")
    print(" " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 62)

    payload = aafl_mrp.build_payload(include_market=not args.no_market, log=print)
    html = aafl_mrp.build_html(payload)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    if args.snapshot:
        try:
            with open(args.snapshot, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
        except OSError:
            pass

    class_a = [r for r in payload["rm"] if r["abc"] == "A"]
    urgent = [r for r in class_a if r["cover"] is not None and r["cover"] < 1]
    print("-" * 62)
    print(" DWH live       :", "YES" if payload["live"] else "NO (static fallback)")
    print(" RM items       :", len(payload["rm"]))
    print(" Class A items  :", len(class_a))
    print(" Class A urgent :", len(urgent), "->", ", ".join(r["name"] for r in urgent))
    print(" Output         :", args.out, "(%d KB)" % (len(html) // 1024))
    print(" Elapsed        : %.1fs" % (time.time() - t0))
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
