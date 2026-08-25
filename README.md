# fragrance finder

One search box. Type `Layton`, see every listing across the discounter sites,
ranked by **landed price per ml**, tap to buy.

Personal use, single user, mobile-first. Static PWA + a scraper that runs on a
cron in GitHub Actions and commits its output.

```
stores.json          which stores, how they ship, do they sell dupes
coupons.json         active codes, by domain
src/shopify.py       polite acquisition from /products.json
src/normalize.py     the parse rules (the hard part)
src/build_index.py   landed cost, ranking, index.json + history.jsonl
test/test_normalize.py
web/                 the PWA (Netlify serves this directory)
data/history.jsonl   append-only price log, one row per fragrance per run
```

## Quickstart

```bash
pip install requests

python3 -m unittest discover -s test          # 40 tests, all must pass

# eyeball one store's parse before trusting it
python3 src/shopify.py aurafragrance.com --name "Aura Fragrance" \
        --sample 50 --sort size_ml --show-title

# full run -> web/data/index.json, web/data/lows.json, data/history.jsonl
python3 src/build_index.py

# serve the app
cd web && python3 -m http.server 8899      # then open http://localhost:8899
```

Offline dry run (no network, uses the checked-in fixtures):

```bash
python3 src/build_index.py --fixtures test/fixtures/offline --no-history \
        --history test/fixtures/offline/SYNTHETIC_history.jsonl
```

## Acquisition

Shopify storefronts expose `/products.json?limit=250&page=N`. The scraper pages
until the `products` array comes back empty. A store with a `collection` set is
scoped to `/collections/<handle>/products.json` instead — required for
multi-category stores like beautyhouse.com, which would otherwise flood the
index with shampoo.

Politeness, non-negotiable: **1 request/second per domain**, descriptive
User-Agent, 3 attempts with 2s/4s backoff, 20s timeout. A store that 404s,
times out, or returns garbage is logged and skipped — one bad store never
aborts the run, and a partial catalogue still beats no catalogue.

Any store with `"platform": "unknown"` is probed with `/products.json?limit=1`.
Valid JSON with a `products` key promotes it to shopify; anything else logs
`NEEDS ADAPTER: <domain>` and the store is skipped for that run.

## Normalization

Parsing happens at the **variant** level, never the product level. A product
routinely bundles a 2ml sample with a 100ml bottle, so a product-level price
means nothing.

| field | rule |
|---|---|
| `brand` | Shopify `vendor`, minus the store's own name, minus junk (`General`, `Shop`, `Default`, `Fragrance`…), then run through a reviewed alias map so one house is one brand — `Christian Dior` and `Dior` are the same key, `Kilian` and `By Kilian` are the same key. |
| `concentration` | `product_type` first (many stores file it correctly there), then a title regex. Normalized to EDT/EDP/EDC/Parfum/Extrait/Elixir/Cologne. **Never guessed** — no match means `null`. |
| `size_ml` | Variant title first, then product title. Explicit ml is trusted; oz is `× 29.5735` then snapped to 50/60/70/75/80/90/100/120/125/150/200 when within 3ml, so `3.4 OZ` is a 100ml bottle rather than 100.55 and `2.0 OZ` is a 60ml rather than 59.1. Entries stay ≥5ml apart: a denser set starts snapping 1.6oz (47.3ml) down to 45 instead of up to its real 50. |
| `condition` | `tester` / `unboxed` / `sealed` / `unknown`. **Display badge only.** Testers are never filtered out — a cheap tester is the good outcome. |
| `preorder` | `pre-order` / `pre order` / `preorder`, including the spaced-hyphen `PRE - ORDER` form. The row is kept and flagged: the price is real, it just is not buyable today. |
| `line` | Title minus brand, size, concentration, condition, preorder marker and filler (`for men`, `spray`, `by <brand>`, a leading `new`…). |

Dropped: the exclusion regexes (gift set, bundle, set of, discovery set, deo,
shower gel, body lotion/cream/oil/wash, candle, diffuser, sample, decant,
travel spray, refill, mini, rollerball, hair mist, perfume oil, membership,
subscription, insurance, add-on), anything under **49ml**, and variants with
`available == false`.

### Canonical key

```
slug(brand) | slug(line) | concentration
```

All sizes collapse into one row — that is the point of the tool. Stores with
`sells_dupes: true` get a `dupe:` prefix, so a clone house never merges with the
designer original.

The brand is stripped from the title **wherever the store put it** — front
(`Dior Sauvage`), tail (`Sauvage Dior`, `Craze Armaf`), after `by`
(`Black Phantom by Kilian`, even though that vendor is literally `By Kilian`),
and repeatedly, since `Chrome Azzaro by Loris Azzaro` needs two passes. Every
contiguous run of vendor tokens is tried longest-first, so `Van Cleef & Arpels`
strips `Van Cleef` rather than a stray `Van`.

The tail case is the dangerous one, because `Bleu de Chanel` also ends in its
brand. A trailing brand is only removed when the word before it is **not** a
connector, which cuts `Sauvage Dior` to `Sauvage` while leaving `Bleu de
Chanel` and `L'Eau d'Issey` whole. Nothing is ever stripped to nothing.

`BRAND_ALIASES` in `normalize.py` is deliberately a **static, hand-checked map**
rather than a rule inferred at runtime: a key that changes when a new store
appears would orphan its own price history. It is also why `Prada` is not
merged into `Agatha Ruiz de la Prada`, which the obvious token-subset rule
would have done.

## Landed cost

```
discount     pct:  price × pct/100
             flat: amount, but only if price ≥ min_spend  (tiered codes)
shipping     0 if subtotal ≥ free_ship_threshold else flat_ship
landed       price − discount + shipping
price_per_ml landed / size_ml          ← the ranking metric
```

Both numbers are stored and both are displayed; sorting is always on per-ml.
Expired coupons are ignored with a warning on stderr. The shipping threshold is
evaluated on the **post-discount** subtotal, matching Shopify's default, and
each listing is costed as a single-bottle order.

`coupons.json`, keyed by domain:

```json
{ "fragrance-nevaeh.com": { "type": "pct",  "code": "20OFFTODAY", "pct": 20, "expires": null },
  "shoparomatix.com":     { "type": "flat", "code": "AROMATIX10", "amount": 10,
                            "min_spend": 180, "expires": "2026-12-31" } }
```

## Output

`web/data/index.json`

```json
{ "generated_at": "…", "stores": [ … ], "items": [
    { "key": "parfums-de-marly|layton|EDP", "brand": "…", "line": "Layton",
      "concentration": "EDP", "listings": [
        { "store": "…", "size_ml": 125, "condition": "tester", "preorder": false,
          "price": 189.99, "landed": 189.73, "price_per_ml": 1.5178, "url": "…" } ] } ] }
```

Listings are sorted by `price_per_ml` ascending.

`data/history.jsonl` gets one appended line per fragrance per run:

```json
{"best_landed":170.14,"best_price_per_ml":1.3611,"best_store":"Fragrance Nevaeh","date":"2026-08-25","key":"parfums-de-marly|layton|EDP"}
```

This is the part that actually saves money. After 90 days it can tell you
whether today's price is a real low or just a Tuesday. `build_index.py` also
derives `web/data/lows.json` (a rolling 90-day low per key) from it, because
Netlify serves `web/` only and the raw log does not belong in the deploy.

## Web app

Dark, mobile-first, no framework, no build step. Loads `index.json` once and
does client-side substring search on brand + line, case- and accent-insensitive
(`immensite` finds `L'Immensité`). Multi-token queries AND together, so
`marly layton` works.

Each result card shows the fragrance, then its listings cheapest-per-ml first —
store, size, condition badge, `$/ml` in the accent colour, landed total, and a
pre-order badge when flagged. The top row is highlighted; tapping any row opens
that product page. When history exists, the card shows `at 90-day low` or
`+N% vs 90d low`.

`manifest.json` and `sw.js` give Add to Home Screen and offline use: the shell
is cache-first, the data is network-first with a cache fallback, so a dead
connection shows the last index with an "Offline" banner instead of a spinner.

## Automation

`.github/workflows/scrape.yml` runs every 6 hours and on manual dispatch. It
runs the test suite first (if the parse rules regress, nothing gets published),
then `build_index.py`, then commits `index.json`, `lows.json` and
`history.jsonl` when they change. Netlify publishes `web/` on push — see
`netlify.toml`.

## Adding a store

Append to `stores.json`:

```json
{ "name": "Some Shop", "domain": "someshop.com", "platform": "shopify",
  "collection": null, "free_ship_threshold": 100, "flat_ship": 8.95,
  "sells_dupes": false, "active": true }
```

Set `"platform": "unknown"` to have the run probe it. Set `collection` if the
store sells more than fragrance. Then eyeball it before trusting it:

```bash
python3 src/shopify.py someshop.com --name "Some Shop" \
        --sample 50 --sort size_ml --show-title
```

`--sort size_ml --show-title` prints the raw product and variant titles beside
each parsed size and lists every non-standard size at the end, which is how you
check size parsing against real titles before trusting any price. The `scrape`
workflow runs the same thing on manual dispatch (`eyeball_domain` input) and
puts it in the run summary.

## Known limitations

- **`flat_ship` is a `$10` placeholder** at five of the six stores — a guess,
  not a researched rate. aurafragrance.com is `0`, meaning known-free rather
  than unchecked. shoparomatix.com has a placeholder rate *and* no known
  free-ship threshold, so it takes +$10 on **every** listing with no way to
  earn it back. Replace the placeholders with real rates and thresholds; one
  line per store in `stores.json`.
- **Some vendor fields are simply wrong**, and no amount of aliasing fixes it:
  `Exclamation for Women by Coty` ships with vendor `Exclamation`, so the brand
  and the line come out swapped. Rows like that index and search fine, they
  just will not merge with the same fragrance carried elsewhere.
- **`olfactoryfactoryllc.com` has never been probed** — no network egress was
  available in the session that built this. The first workflow run either
  promotes it to shopify or logs `NEEDS ADAPTER`.
- **The snap set stops at 200ml**, per spec, so an 8.4oz bottle records as
  248.4ml rather than 250. Add `250` to `STANDARD_SIZES` in `normalize.py` if
  you want it snapped.
- **Brand falls back to `null`** when `vendor` is empty or is just the store's
  own name. Those rows still index, they just will not merge with the same
  fragrance carried under a real vendor string.
- **Flankers still merge into their base fragrance.** `Acqua di Gio Elixir for
  Men EDP` parses to `concentration=EDP` and `line=Acqua di Gio`, because
  `Elixir` is in the concentration vocabulary and `line` is the title minus the
  concentration — so it shares a key with plain Acqua di Gio EDP. The same goes
  for `Intense`, `Extreme`, `Absolu`. Fixing it means deciding when those words
  name a flanker rather than a strength; left open deliberately.
- **Renaming keys orphans their history.** `data/history.jsonl` is keyed by the
  canonical key, so a normalization change that renames a key restarts its
  90-day low. Worth remembering before editing `BRAND_ALIASES` once real
  history has accumulated.
