"""Variant-level normalization for Shopify fragrance listings.

Everything here is pure: dicts in, dicts out, no network, no I/O. That is what
makes the rules testable, and the rules are the whole ballgame -- a product-level
price is meaningless when a product bundles a 2ml sample with a 100ml bottle.

Public surface:
    slugify(s)                          -> str
    parse_size_ml(*texts)               -> int | float | None
    parse_concentration(ptype, *titles) -> str | None
    parse_condition(*texts)             -> "tester"|"unboxed"|"sealed"|"unknown"
    parse_preorder(*texts)              -> bool
    excluded_reason(*texts)             -> str | None
    clean_brand(vendor, store_name)     -> str | None
    parse_line(title, brand, conc)      -> str
    canonical_key(brand, line, conc, sells_dupes) -> str
    normalize_variant(product, variant, store)    -> dict | None
    normalize_variant_ex(...)                     -> (dict | None, reason | None)
"""
from __future__ import annotations

import re
import unicodedata

# --- constants ---------------------------------------------------------------

OZ_TO_ML = 29.5735
STANDARD_SIZES = (50, 75, 100, 125, 150, 200)
SNAP_TOLERANCE_ML = 3.0
SIZE_FLOOR_ML = 49.0

# Not fragrance bottles. Matched against product title AND variant title.
EXCLUDE_PATTERNS = [
    ("gift set", r"gift\s*set"),
    ("discovery set", r"discovery\s*set"),
    ("set of", r"\bset\s+of\b"),
    ("bundle", r"\bbundles?\b"),
    ("deodorant", r"\bdeodorants?\b|\bdeo\b"),
    ("shower gel", r"shower\s*gel"),
    ("body lotion", r"body\s*lotion"),
    ("body cream", r"body\s*cream"),
    ("body oil", r"body\s*oil"),
    ("body wash", r"body\s*wash"),
    ("candle", r"\bcandles?\b"),
    ("diffuser", r"\bdiffusers?\b"),
    ("sample", r"\bsamples?\b"),
    ("decant", r"\bdecants?\b"),
    ("travel spray", r"travel\s*spray"),
    ("refill", r"\brefills?\b"),
    ("mini", r"\bminis?\b|\bminiatures?\b"),
    ("rollerball", r"\brollerball\b|\broll[-\s]?on\b"),
    ("hair mist", r"hair\s*mist"),
    ("perfume oil", r"perfume\s*oil"),
    ("membership", r"\bmembership\b"),
    ("subscription", r"\bsubscriptions?\b"),
    ("insurance", r"\binsurance\b"),
    ("add-on", r"\badd[-\s]?ons?\b"),
]
EXCLUDE_RE = [(name, re.compile(pat, re.I)) for name, pat in EXCLUDE_PATTERNS]

# Ordered: first match wins, so the "eau de X" long forms are tested before the
# bare words they contain.
CONCENTRATION_RULES = [
    ("Extrait", r"extrait\s*(?:de\s*parfum)?"),
    ("EDP", r"eau\s*de\s*parfum|\bedp\b|\be\.d\.p\.?\b"),
    ("EDT", r"eau\s*de\s*toilette|\bedt\b|\be\.d\.t\.?\b"),
    ("EDC", r"eau\s*de\s*cologne|\bedc\b|\be\.d\.c\.?\b"),
    ("Elixir", r"\belixir\b"),
    ("Cologne", r"\bcolognes?\b"),
    ("Parfum", r"\bparfum\b|\bpure\s*perfume\b|\bperfume\s*extract\b"),
]
CONCENTRATION_RE = [(name, re.compile(pat, re.I)) for name, pat in CONCENTRATION_RULES]

# Condition: tester is checked first on purpose -- "Tester Box" contains "box".
CONDITION_RULES = [
    ("tester", r"\btesters?\b|\btstr\b"),
    ("unboxed", r"\bunboxed\b|\bno\s*box\b|\bwithout\s*box\b|\bdamaged\s*box\b"),
    ("sealed", r"\bsealed\b|\bnew\s*in\s*box\b|\bnib\b|\bregular\s*box\b|\bwith\s*box\b|\bboxed\b"),
]
CONDITION_RE = [(name, re.compile(pat, re.I)) for name, pat in CONDITION_RULES]

PREORDER_RE = re.compile(r"\bpre\s*[-‐-―]?\s*orders?\b", re.I)

SIZE_ML_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:ml\b|ml\.|milli\s*lit(?:er|re)s?\b)", re.I)
SIZE_OZ_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:fl\.?\s*)?(?:oz\b|oz\.|ounces?\b)", re.I)

# Words that carry no identity. Stripped when building `line`.
FILLER_PATTERNS = [
    r"\bfor\s+men\b", r"\bfor\s+women\b", r"\bfor\s+everyone\b",
    r"\bfor\s+him\b", r"\bfor\s+her\b", r"\bfor\s+unisex\b", r"\bunisex\b",
    r"\bmens\b", r"\bwomens\b", r"\bmen'?s\b", r"\bwomen'?s\b",
    r"\bsprays?\b", r"\bperfumes?\b", r"\bfragrances?\b", r"\bbottle\b",
    r"\bauthentic\b", r"\b100%\s*original\b", r"\bbrand\s*new\b",
]
FILLER_RE = [re.compile(p, re.I) for p in FILLER_PATTERNS]

# Condition/packaging noise removed from `line` (broader than CONDITION_RULES:
# here we also drop the bare "box" left behind by "Tester Box").
LINE_NOISE_RE = [re.compile(p, re.I) for p in [
    r"\btesters?\b", r"\bunboxed\b", r"\bno\s*box\b", r"\bwithout\s*box\b",
    r"\bsealed\b", r"\bnew\s*in\s*box\b", r"\bnib\b",
    r"\b(?:regular|plainer|original|damaged|with)\s*box(?:es|ed)?\b", r"\bbox(?:es|ed)?\b",
    r"\bsame\s*liquid\b", r"\bin\s*stock\b", r"\bfree\s*shipping\b",
]]

# Orphaned connectors left dangling after removals ("Libre Le Parfum" -> "Libre Le").
TRAILING_ORPHAN_RE = re.compile(
    r"[\s,\-/|]+(?:the|le|la|les|de|du|des|d|di|for|by|a|an|in|of)\.?$", re.I)
LEADING_ORPHAN_RE = re.compile(r"^(?:the|by|de|of)\b[\s,\-/|]*", re.I)


# --- small helpers -----------------------------------------------------------

def strip_accents(text: str) -> str:
    """NFD-decompose and drop combining marks, so `Immensité` == `Immensite`."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def slugify(text: str | None) -> str:
    folded = strip_accents(text or "").lower()
    folded = re.sub(r"[^a-z0-9]+", "-", folded)
    return folded.strip("-")


def _tidy(text: str) -> str:
    """Collapse the punctuation debris left behind by regex removals."""
    text = re.sub(r"\(\s*[-–—,/|]*\s*\)", " ", text)   # empty ( ) or ( - )
    text = re.sub(r"\[\s*[-–—,/|]*\s*\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([,/|])\s*\1+", r"\1", text)
    text = text.strip(" \t-–—,/|:;.&+")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _first_text(*texts: str | None) -> list[str]:
    return [t for t in texts if t and t.strip()]


# --- field parsers -----------------------------------------------------------

def _snap(ml: float) -> int | float:
    """Snap an oz-derived size onto a standard bottle size when it is close."""
    best = min(STANDARD_SIZES, key=lambda s: abs(s - ml))
    if abs(best - ml) <= SNAP_TOLERANCE_ML:
        return best
    return round(ml, 1)


def _as_number(raw: str) -> float:
    return float(raw.replace(",", "."))


def parse_size_ml(*texts: str | None) -> int | float | None:
    """Size in ml, reading the given texts in order (variant title first).

    Explicit ml is trusted as printed. oz is converted and then snapped onto the
    nearest standard size when within 3ml -- 3.4oz is a 100ml bottle, not 100.55.
    """
    for text in _first_text(*texts):
        m = SIZE_ML_RE.search(text)
        if m:
            ml = _as_number(m.group(1))
            return int(ml) if float(ml).is_integer() else round(ml, 1)
        m = SIZE_OZ_RE.search(text)
        if m:
            return _snap(_as_number(m.group(1)) * OZ_TO_ML)
    return None


def parse_concentration(product_type: str | None, *titles: str | None) -> str | None:
    """Shopify `product_type` first -- many stores file it correctly there --
    then the titles. Returns None rather than guessing."""
    for text in _first_text(product_type, *titles):
        for name, rx in CONCENTRATION_RE:
            if rx.search(text):
                return name
    return None


def parse_condition(*texts: str | None) -> str:
    """Display badge only. Never used to filter: a cheap tester is the good outcome."""
    blob = " ".join(_first_text(*texts))
    for name, rx in CONDITION_RE:
        if rx.search(blob):
            return name
    return "unknown"


def parse_preorder(*texts: str | None) -> bool:
    blob = " ".join(_first_text(*texts))
    return bool(PREORDER_RE.search(blob))


def excluded_reason(*texts: str | None) -> str | None:
    blob = " ".join(_first_text(*texts))
    for name, rx in EXCLUDE_RE:
        if rx.search(blob):
            return name
    return None


def clean_brand(vendor: str | None, store_name: str | None = None,
                store_domain: str | None = None) -> str | None:
    """Shopify `vendor`, minus the junk stores park in that field."""
    if not vendor:
        return None
    brand = re.sub(r"\bgeneral\b", " ", vendor, flags=re.I)
    if store_name:
        brand = re.sub(re.escape(store_name), " ", brand, flags=re.I)
    brand = _tidy(brand)
    if not brand:
        return None
    dead = {slugify(store_name), slugify(store_domain or "").replace("-com", ""),
            "general", "", "n-a", "none", "no-brand", "unbranded"}
    if slugify(brand) in dead:
        return None
    return brand


CONNECTOR_TOKENS = {"de", "du", "des", "di", "of", "and", "the", "le", "la",
                    "les", "by", "&", "d", "el", "al"}


def _brand_prefix_candidates(brand: str) -> list[str]:
    """Every contiguous run of brand tokens, longest first.

    Stores rarely print the vendor string verbatim: vendor "Christian Dior"
    fronts a title as "Dior ...", vendor "Initio Parfums Prives" as "Initio ...".
    Runs (rather than single tokens) keep "Van Cleef & Arpels" -> "Van Cleef"
    from being chopped down to a stray "Cleef".
    """
    tokens = [t for t in re.split(r"\s+", brand.strip()) if t]
    candidates = []
    for i in range(len(tokens)):
        for j in range(len(tokens), i, -1):
            run = tokens[i:j]
            if all(t.lower().strip(".&") in CONNECTOR_TOKENS or len(t) < 2 for t in run):
                continue
            candidates.append(" ".join(run))
    return sorted(set(candidates), key=len, reverse=True)


def _strip_brand_prefix(text: str, brand: str) -> str:
    """Strip the brand only where it *fronts* the title.

    Prefix-only on purpose: "Bleu de Chanel" has to survive brand == "Chanel".
    """
    for candidate in _brand_prefix_candidates(brand):
        stripped = re.sub(r"^\s*" + re.escape(candidate) + r"\b[\s,\-–—:|]*",
                          " ", text, flags=re.I)
        if stripped != text and _tidy(stripped):     # never strip away the whole name
            return stripped
    return text


def parse_line(product_title: str, brand: str | None,
               concentration: str | None = None,
               variant_title: str | None = None) -> str:
    """The fragrance name, with everything that is not identity removed."""
    text = product_title or ""
    text = re.sub(r"^\s*(?:brand\s+)?new\b[\s,\-–—:]*", " ", text, flags=re.I)
    text = PREORDER_RE.sub(" ", text)

    if brand:
        text = re.sub(r"\bby\s+" + re.escape(brand) + r"\b", " ", text, flags=re.I)
        text = _strip_brand_prefix(text, brand)

    text = SIZE_ML_RE.sub(" ", text)
    text = SIZE_OZ_RE.sub(" ", text)
    for _, rx in CONCENTRATION_RE:
        text = rx.sub(" ", text)
    for rx in LINE_NOISE_RE:
        text = rx.sub(" ", text)
    for rx in FILLER_RE:
        text = rx.sub(" ", text)
    text = re.sub(r"\bnew\b[\s,\-–—:]*$", " ", text, flags=re.I)

    text = _tidy(text)
    text = LEADING_ORPHAN_RE.sub("", text)
    prev = None
    while prev != text:                      # peel one orphan per pass
        prev = text
        text = _tidy(TRAILING_ORPHAN_RE.sub("", text))
    return text


def canonical_key(brand: str | None, line: str | None,
                  concentration: str | None, sells_dupes: bool = False) -> str:
    """slug(brand) | slug(line) | concentration -- all sizes collapse into one row.

    Dupe houses get a `dupe:` prefix so a clone never merges with the original.
    """
    key = f"{slugify(brand)}|{slugify(line)}|{concentration or ''}"
    return f"dupe:{key}" if sells_dupes else key


# --- the pipeline ------------------------------------------------------------

def normalize_variant_ex(product: dict, variant: dict, store: dict):
    """Returns (row, None) or (None, drop_reason). Reasons feed the run log."""
    product_title = (product.get("title") or "").strip()
    variant_title = (variant.get("title") or "").strip()
    if variant_title.lower() in ("default title", "default"):
        variant_title_for_size = ""
    else:
        variant_title_for_size = variant_title

    if variant.get("available") is False:
        return None, "unavailable"

    reason = excluded_reason(product_title, variant_title)
    if reason:
        return None, f"excluded:{reason}"

    size_ml = parse_size_ml(variant_title_for_size, product_title)
    if size_ml is None:
        return None, "no_size"
    if float(size_ml) < SIZE_FLOOR_ML:
        return None, f"below_size_floor:{float(size_ml)}"

    try:
        price = float(str(variant.get("price", "")).replace(",", ""))
    except (TypeError, ValueError):
        return None, "no_price"
    if price <= 0:
        return None, "no_price"

    store_name = store.get("name")
    brand = clean_brand(product.get("vendor"), store_name, store.get("domain"))
    concentration = parse_concentration(
        product.get("product_type"), variant_title, product_title)
    line = parse_line(product_title, brand, concentration, variant_title)
    if not line:
        return None, "no_line"

    handle = product.get("handle") or ""
    url = f"https://{store.get('domain')}/products/{handle}"
    if variant.get("id"):
        url += f"?variant={variant['id']}"

    row = {
        "key": canonical_key(brand, line, concentration, bool(store.get("sells_dupes"))),
        "store": store_name,
        "store_domain": store.get("domain"),
        "brand": brand,
        "line": line,
        "concentration": concentration,
        "size_ml": size_ml,
        "condition": parse_condition(variant_title, product_title),
        "preorder": parse_preorder(variant_title, product_title),
        "price": round(price, 2),
        "url": url,
        "product_title": product_title,
        "variant_title": variant_title,
    }
    return row, None


def normalize_variant(product: dict, variant: dict, store: dict) -> dict | None:
    return normalize_variant_ex(product, variant, store)[0]


def normalize_product(product: dict, store: dict):
    """Every in-scope variant of one product, plus the drop reasons."""
    rows, drops = [], []
    for variant in product.get("variants") or []:
        row, reason = normalize_variant_ex(product, variant, store)
        (rows if row else drops).append(row or reason)
    return rows, drops
