"""Polite Shopify storefront acquisition.

Shopify exposes /products.json?limit=250&page=N on any storefront, and
/collections/<handle>/products.json for a scoped subset -- required for stores
that also sell makeup, skincare and hair.

Rules that matter more than speed:
  * 1 request/second per domain
  * descriptive User-Agent
  * 3 retries with exponential backoff, 20s timeout
  * one bad store must never abort the run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import STANDARD_SIZES, normalize_variant_ex  # noqa: E402

USER_AGENT = (
    "fragrance-finder/1.0 (+https://github.com/cvelazquez1123/forking-lesson) "
    "personal price-comparison bot; 1 req/sec; contact via GitHub issues"
)
TIMEOUT = 20
RETRIES = 3
RATE_LIMIT_SECONDS = 1.0
PAGE_SIZE = 250
MAX_PAGES = 40           # 10k products; a hard stop against a paging loop

_last_request_at: dict[str, float] = {}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class FetchError(Exception):
    pass


def _throttle(domain: str) -> None:
    last = _last_request_at.get(domain)
    if last is not None:
        wait = RATE_LIMIT_SECONDS - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[domain] = time.monotonic()


def fetch_json(url: str, session: requests.Session | None = None) -> dict:
    """GET one URL politely. Raises FetchError after RETRIES failed attempts."""
    domain = urllib.parse.urlparse(url).netloc
    session = session or requests
    last_error = None
    for attempt in range(1, RETRIES + 1):
        _throttle(domain)
        try:
            resp = session.get(url, timeout=TIMEOUT,
                               headers={"User-Agent": USER_AGENT,
                                        "Accept": "application/json"})
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 404:
                raise FetchError(f"404 {url}")          # not retryable
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}"
            elif not resp.ok:
                raise FetchError(f"HTTP {resp.status_code} {url}")
            else:
                try:
                    return resp.json()
                except ValueError:
                    raise FetchError(f"non-JSON response from {url}")
        if attempt < RETRIES:
            backoff = 2 ** attempt
            log(f"    retry {attempt}/{RETRIES - 1} in {backoff}s ({last_error}) {url}")
            time.sleep(backoff)
    raise FetchError(f"{last_error} after {RETRIES} attempts: {url}")


def products_url(domain: str, collection: str | None, page: int,
                 limit: int = PAGE_SIZE) -> str:
    base = f"https://{domain}"
    path = f"/collections/{collection}/products.json" if collection else "/products.json"
    return f"{base}{path}?limit={limit}&page={page}"


def probe(domain: str, session: requests.Session | None = None) -> str | None:
    """Return "shopify" if /products.json?limit=1 is valid JSON with a products key."""
    url = products_url(domain, None, 1, limit=1)
    try:
        payload = fetch_json(url, session)
    except FetchError as exc:
        log(f"  probe failed: {exc}")
        return None
    if isinstance(payload, dict) and isinstance(payload.get("products"), list):
        return "shopify"
    return None


def iter_products(domain: str, collection: str | None = None,
                  session: requests.Session | None = None,
                  max_pages: int = MAX_PAGES):
    """Yield raw product dicts, paging until the products array comes back empty."""
    seen_first_ids = set()
    for page in range(1, max_pages + 1):
        url = products_url(domain, collection, page)
        payload = fetch_json(url, session)
        products = (payload or {}).get("products") or []
        if not products:
            return
        first_id = products[0].get("id")
        if first_id in seen_first_ids:
            log(f"  {domain}: page {page} repeats page 1 -- storefront ignores ?page, stopping")
            return
        seen_first_ids.add(first_id)
        log(f"  {domain}: page {page} -> {len(products)} products")
        yield from products
    log(f"  {domain}: hit MAX_PAGES={max_pages}, stopping (catalog may be truncated)")


def scrape_store(store: dict, session: requests.Session | None = None):
    """All normalized rows for one store. Returns (rows, stats). Never raises."""
    domain = store["domain"]
    stats = {"domain": domain, "products": 0, "variants": 0, "rows": 0,
             "drops": {}, "error": None}
    rows: list[dict] = []

    platform = store.get("platform") or "shopify"
    if platform == "unknown":
        detected = probe(domain, session)
        if detected != "shopify":
            log(f"NEEDS ADAPTER: {domain}")
            stats["error"] = "needs_adapter"
            return rows, stats
        log(f"  {domain}: probe says shopify")
        store = dict(store, platform="shopify")
        stats["detected_platform"] = "shopify"
    elif platform != "shopify":
        log(f"NEEDS ADAPTER: {domain} (platform={platform})")
        stats["error"] = "needs_adapter"
        return rows, stats

    try:
        for product in iter_products(domain, store.get("collection"), session):
            stats["products"] += 1
            for variant in product.get("variants") or []:
                stats["variants"] += 1
                row, reason = normalize_variant_ex(product, variant, store)
                if row:
                    rows.append(row)
                else:
                    bucket = reason.split(":")[0]
                    stats["drops"][bucket] = stats["drops"].get(bucket, 0) + 1
    except FetchError as exc:
        # A partial catalog still beats aborting the whole run.
        log(f"  {domain}: FETCH FAILED -- {exc}")
        stats["error"] = str(exc)
    except Exception as exc:                              # noqa: BLE001
        log(f"  {domain}: UNEXPECTED ERROR -- {type(exc).__name__}: {exc}")
        stats["error"] = f"{type(exc).__name__}: {exc}"

    stats["rows"] = len(rows)
    return rows, stats


def load_fixture(path: str):
    """Offline source of raw products.json payloads, for eyeballing the parser."""
    with open(path) as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        return payload.get("products") or []
    return payload


def print_rows(rows, show_title: bool = False) -> None:
    """Eyeball table. A `*` marks a size that is not a standard bottle."""
    header = (f"{'BRAND':<20} {'LINE':<24} {'CONC':<7} {'SIZE':>8} "
              f"{'COND':<8} {'PRE':<4} {'PRICE':>9}")
    print(header)
    print("-" * len(header))
    for row in rows:
        odd = "*" if float(row["size_ml"]) not in STANDARD_SIZES else " "
        print(f"{(row['brand'] or '-'):<20.20} {row['line']:<24.24} "
              f"{(row['concentration'] or '-'):<7} "
              f"{str(row['size_ml']) + 'ml':>7}{odd} "
              f"{row['condition']:<8} {('YES' if row['preorder'] else '-'):<4} "
              f"${row['price']:>8.2f}")
        if show_title:
            print(f"{'':<20} raw: {row['product_title']!r}")
            print(f"{'':<20}      variant={row['variant_title']!r}")
    print("-" * len(header))


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape one Shopify store and print parsed rows.")
    ap.add_argument("domain")
    ap.add_argument("--collection", default=None)
    ap.add_argument("--name", default=None, help="store display name (for brand cleaning)")
    ap.add_argument("--sells-dupes", action="store_true")
    ap.add_argument("--sample", type=int, default=20, help="rows to print (0 = all)")
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--sort", choices=("size_ml", "price", "brand"), default=None,
                    help="sort the printed rows (default: catalogue order)")
    ap.add_argument("--show-title", action="store_true",
                    help="print the raw product/variant titles under each row")
    ap.add_argument("--fixture", default=None,
                    help="read products.json from a local file instead of the network")
    args = ap.parse_args()

    store = {"name": args.name or args.domain, "domain": args.domain,
             "platform": "shopify", "collection": args.collection,
             "sells_dupes": args.sells_dupes}

    if args.probe_only:
        print(probe(args.domain) or "NEEDS ADAPTER")
        return 0

    if args.fixture:
        products = load_fixture(args.fixture)
        rows, stats = [], {"products": 0, "variants": 0, "drops": {}}
        for product in products:
            stats["products"] += 1
            for variant in product.get("variants") or []:
                stats["variants"] += 1
                row, reason = normalize_variant_ex(product, variant, store)
                if row:
                    rows.append(row)
                else:
                    bucket = reason.split(":")[0]
                    stats["drops"][bucket] = stats["drops"].get(bucket, 0) + 1
        stats["rows"] = len(rows)
    else:
        with requests.Session() as session:
            rows, stats = scrape_store(store, session)

    if args.sort == "size_ml":
        rows.sort(key=lambda r: (float(r["size_ml"]), (r["brand"] or "").lower(), r["line"].lower()))
    elif args.sort == "price":
        rows.sort(key=lambda r: r["price"])
    elif args.sort == "brand":
        rows.sort(key=lambda r: ((r["brand"] or "~").lower(), r["line"].lower()))

    limit = len(rows) if args.sample == 0 else args.sample
    print_rows(rows[:limit], show_title=args.show_title)

    print(f"{stats['products']} products -> {stats['variants']} variants -> "
          f"{stats['rows']} rows kept; dropped: "
          f"{json.dumps(stats['drops'], sort_keys=True)}")

    odd = [r for r in rows if float(r["size_ml"]) not in STANDARD_SIZES]
    print(f"\nNON-STANDARD SIZES: {len(odd)} of {len(rows)} rows "
          f"(anything not in {'/'.join(str(s) for s in STANDARD_SIZES)}ml)")
    for row in odd[:40]:
        print(f"  {str(row['size_ml']) + 'ml':>9}  <- product: {row['product_title']!r}")
        print(f"  {'':>9}     variant: {row['variant_title']!r}")
    if len(odd) > 40:
        print(f"  ... and {len(odd) - 40} more")

    if stats.get("error"):
        print(f"error: {stats['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
