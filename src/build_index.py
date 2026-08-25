"""Scrape every active store, compute landed cost, emit web/data/index.json.

Landed cost is the whole point: a $118 bottle with $15 shipping loses to a $125
bottle that ships free, and neither of those numbers is the one you rank on --
price_per_ml is.

    landed = price - discount + shipping
    shipping = 0 if subtotal >= free_ship_threshold else flat_ship
    price_per_ml = landed / size_ml          <- the ranking metric

Also appends one row per canonical key per run to data/history.jsonl. That log
is the part that actually saves money: after 90 days it can tell you whether
today's price is a real low or just a Tuesday.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shopify  # noqa: E402
from shopify import log  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_WINDOW_DAYS = 90


# --- coupons -----------------------------------------------------------------

def coupon_for(domain: str, coupons: dict, today: dt.date):
    """The live coupon for a domain, or None. Expired codes are ignored loudly."""
    coupon = (coupons or {}).get(domain)
    if not coupon:
        return None
    expires = coupon.get("expires")
    if expires:
        try:
            if dt.date.fromisoformat(str(expires)[:10]) < today:
                log(f"WARNING: coupon {coupon.get('code')} for {domain} expired "
                    f"{expires} -- ignoring")
                return None
        except ValueError:
            log(f"WARNING: coupon {coupon.get('code')} for {domain} has an "
                f"unparseable expires={expires!r} -- ignoring the coupon")
            return None
    if coupon.get("type") not in ("pct", "flat"):
        log(f"WARNING: coupon for {domain} has unknown type={coupon.get('type')!r} "
            f"-- ignoring")
        return None
    return coupon


def discount_for(price: float, coupon: dict | None) -> float:
    if not coupon:
        return 0.0
    if coupon["type"] == "pct":
        return round(price * float(coupon.get("pct") or 0) / 100.0, 2)
    # flat, possibly tiered behind a minimum spend
    min_spend = coupon.get("min_spend")
    if min_spend is not None and price < float(min_spend):
        return 0.0
    return round(min(float(coupon.get("amount") or 0), price), 2)


def landed_cost(price: float, store: dict, coupon: dict | None) -> tuple[float, float, float]:
    """Returns (landed, discount, shipping) for a single-bottle order."""
    discount = discount_for(price, coupon)
    subtotal = round(price - discount, 2)
    flat_ship = store.get("flat_ship")
    threshold = store.get("free_ship_threshold")
    if flat_ship is None:
        shipping = 0.0            # unknown ship cost -- see README, fill flat_ship in
    elif threshold is None:
        shipping = float(flat_ship)
    else:
        shipping = 0.0 if subtotal >= float(threshold) else float(flat_ship)
    return round(subtotal + shipping, 2), discount, shipping


# --- assembly ----------------------------------------------------------------

def _consensus(values):
    """Most common spelling, longest wins ties -- stores disagree on casing."""
    counts = Counter(v for v in values if v)
    if not counts:
        return None
    top = max(counts.values())
    return sorted((v for v, c in counts.items() if c == top), key=len, reverse=True)[0]


def build_items(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["key"]].append(row)

    items = []
    for key, group in grouped.items():
        listings = sorted(
            ({
                "store": r["store"],
                "size_ml": r["size_ml"],
                "condition": r["condition"],
                "preorder": r["preorder"],
                "price": r["price"],
                "landed": r["landed"],
                "price_per_ml": r["price_per_ml"],
                "url": r["url"],
            } for r in group),
            key=lambda listing: listing["price_per_ml"])
        items.append({
            "key": key,
            "brand": _consensus(r["brand"] for r in group),
            "line": _consensus(r["line"] for r in group),
            "concentration": _consensus(r["concentration"] for r in group),
            "listings": listings,
        })
    items.sort(key=lambda i: ((i["brand"] or "~").lower(), (i["line"] or "").lower()))
    return items


def read_history(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    entries = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def append_history(path: str, items: list[dict], today: str) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    written = 0
    with open(path, "a") as fh:
        for item in items:
            best = item["listings"][0]
            fh.write(json.dumps({
                "date": today,
                "key": item["key"],
                "best_price_per_ml": best["price_per_ml"],
                "best_store": best["store"],
                "best_landed": best["landed"],
            }, sort_keys=True) + "\n")
            written += 1
    return written


def compute_lows(history: list[dict], today: dt.date,
                 window_days: int = HISTORY_WINDOW_DAYS) -> dict:
    """Per-key rolling low, for the web app's 'vs 90-day low' line."""
    cutoff = today - dt.timedelta(days=window_days)
    lows: dict[str, dict] = {}
    for entry in history:
        try:
            date = dt.date.fromisoformat(str(entry.get("date"))[:10])
        except (ValueError, TypeError):
            continue
        if date < cutoff:
            continue
        key, ppml = entry.get("key"), entry.get("best_price_per_ml")
        if not key or not isinstance(ppml, (int, float)):
            continue
        current = lows.get(key)
        if current is None:
            lows[key] = {"low": ppml, "low_date": date.isoformat(), "samples": 1,
                         "first_seen": date.isoformat()}
            continue
        current["samples"] += 1
        if date.isoformat() < current["first_seen"]:
            current["first_seen"] = date.isoformat()
        if ppml < current["low"]:
            current["low"] = ppml
            current["low_date"] = date.isoformat()
    return lows


# --- run ---------------------------------------------------------------------

def run(stores_path: str, coupons_path: str, out_path: str, history_path: str,
        lows_path: str, fixtures_dir: str | None = None,
        write_history: bool = True) -> int:
    with open(stores_path) as fh:
        stores = json.load(fh)
    with open(coupons_path) as fh:
        coupons = json.load(fh)

    today = dt.date.today()
    all_rows: list[dict] = []
    store_report = []

    session = None
    if fixtures_dir is None:
        import requests
        session = requests.Session()

    for store in stores:
        if not store.get("active", True):
            log(f"{store['domain']}: inactive, skipping")
            continue
        log(f"{store['domain']}: starting"
            + (f" (collection={store['collection']})" if store.get("collection") else ""))

        if fixtures_dir:
            path = os.path.join(fixtures_dir, f"{store['domain']}.json")
            if not os.path.exists(path):
                log(f"  {store['domain']}: no fixture at {path}, skipping")
                store_report.append({
                    "name": store["name"], "domain": store["domain"],
                    "collection": store.get("collection"),
                    "free_ship_threshold": store.get("free_ship_threshold"),
                    "flat_ship": store.get("flat_ship"),
                    "sells_dupes": bool(store.get("sells_dupes")),
                    "coupon": None, "products": 0, "rows": 0, "error": "no_fixture"})
                continue
            rows, stats = [], {"products": 0, "variants": 0, "drops": {}, "error": None}
            for product in shopify.load_fixture(path):
                stats["products"] += 1
                for variant in product.get("variants") or []:
                    stats["variants"] += 1
                    row, reason = shopify.normalize_variant_ex(product, variant, store)
                    if row:
                        rows.append(row)
                    else:
                        bucket = reason.split(":")[0]
                        stats["drops"][bucket] = stats["drops"].get(bucket, 0) + 1
            stats["rows"] = len(rows)
        else:
            rows, stats = shopify.scrape_store(store, session)

        coupon = coupon_for(store["domain"], coupons, today)
        for row in rows:
            landed, discount, shipping = landed_cost(row["price"], store, coupon)
            row["landed"] = landed
            row["discount"] = discount
            row["shipping"] = shipping
            row["price_per_ml"] = round(landed / float(row["size_ml"]), 4)
        all_rows.extend(rows)

        log(f"  {store['domain']}: {stats.get('products', 0)} products -> "
            f"{stats.get('variants', 0)} variants -> {len(rows)} rows; "
            f"dropped {json.dumps(stats.get('drops', {}), sort_keys=True)}")
        store_report.append({
            "name": store["name"], "domain": store["domain"],
            "collection": store.get("collection"),
            "free_ship_threshold": store.get("free_ship_threshold"),
            "flat_ship": store.get("flat_ship"),
            "sells_dupes": bool(store.get("sells_dupes")),
            "coupon": (coupon or {}).get("code"),
            "products": stats.get("products", 0),
            "rows": len(rows),
            "error": stats.get("error"),
        })

    items = build_items(all_rows)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    if write_history and items:
        written = append_history(history_path, items, today.isoformat())
        log(f"history: appended {written} rows to {history_path}")

    lows = compute_lows(read_history(history_path), today)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        payload = {"generated_at": now, "stores": store_report, "items": items}
        if fixtures_dir:
            payload["demo"] = True      # built from fixtures, not a live scrape
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    with open(lows_path, "w") as fh:
        json.dump({"generated_at": now, "window_days": HISTORY_WINDOW_DAYS,
                   "lows": lows}, fh, ensure_ascii=False, separators=(",", ":"))

    ok = sum(1 for s in store_report if not s.get("error"))
    log(f"\nDONE: {len(items)} fragrances / {len(all_rows)} listings from "
        f"{ok}/{len(store_report)} stores -> {out_path}")
    for s in store_report:
        if s.get("error"):
            log(f"  degraded: {s['domain']} -- {s['error']}")
    return 0 if items else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stores", default=os.path.join(ROOT, "stores.json"))
    ap.add_argument("--coupons", default=os.path.join(ROOT, "coupons.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "web", "data", "index.json"))
    ap.add_argument("--history", default=os.path.join(ROOT, "data", "history.jsonl"))
    ap.add_argument("--lows", default=os.path.join(ROOT, "web", "data", "lows.json"))
    ap.add_argument("--fixtures", default=None,
                    help="offline: read <domain>.json fixtures from this directory")
    ap.add_argument("--no-history", action="store_true",
                    help="build index.json without appending to history.jsonl")
    args = ap.parse_args()
    return run(args.stores, args.coupons, args.out, args.history, args.lows,
               args.fixtures, write_history=not args.no_history)


if __name__ == "__main__":
    raise SystemExit(main())
