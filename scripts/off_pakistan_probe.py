"""Feasibility probe: how much Pakistani product data exists in the Open Food Facts family?

READ-ONLY w.r.t. the app: touches no app code, DB or migrations. Only calls public APIs and
writes two files:
    docs/off_pakistan_probe_report.md
    docs/off_pakistan_items.csv

Databases probed (public v2 APIs, no key):
    off  - Open Food Facts      https://world.openfoodfacts.org
    obf  - Open Beauty Facts    https://world.openbeautyfacts.org
    opf  - Open Products Facts  https://world.openproductsfacts.org

Discovery per database:
    (a) v2 search  countries_tags=en:pakistan, paged.
    (b) barcodes starting with 896 (GS1 Pakistan prefix). The v2 search API has no
        code-prefix filter, so:
          * all DBs: 896-prefixed codes found inside the (a) results are counted;
          * off only: Search-a-licious (search.openfoodfacts.org, `code:896*`) is used as a
            supplementary prefix query, which also catches 896 products not tagged Pakistan.
        A full scan of obf/opf (~80k / ~50k products) is deliberately NOT done (would take
        many minutes at 1 req/s and is unfriendly to a free service).

Politeness: identifying User-Agent, <= 1 request/second (global), backoff on 429/503/5xx.

Usage:
    python scripts/off_pakistan_probe.py [--max-pages N] [--sample 40] [--seed 1]
Env:
    PROBE_CONTACT   contact email/URL put in the User-Agent (default: placeholder)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

CONTACT = os.environ.get("PROBE_CONTACT", "set-PROBE_CONTACT@example.invalid")
USER_AGENT = f"PakPOS-catalog-probe/1.0 (contact: {CONTACT})"

DBS = {
    "off": "https://world.openfoodfacts.org",
    "obf": "https://world.openbeautyfacts.org",
    "opf": "https://world.openproductsfacts.org",
}
DB_NAMES = {"off": "Open Food Facts", "obf": "Open Beauty Facts", "opf": "Open Products Facts"}
SAL_URL = "https://search.openfoodfacts.org/search"

MIN_INTERVAL = 1.05  # seconds between ANY two requests (>= 1s)
MAX_RETRIES = 6
PAGE_SIZE = 100
FIELDS = "code,product_name,brands,quantity,categories,categories_tags,image_url,image_front_url,countries_tags"


# ----------------------------------------------------------------------------- HTTP
class Http:
    def __init__(self) -> None:
        self._last = 0.0
        self.requests = 0
        self.errors: list[str] = []          # human-readable error log
        self.status_counts: Counter[str] = Counter()

    def _wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - delta)
        self._last = time.monotonic()

    def get_json(self, url: str, label: str = "") -> tuple[dict | None, dict]:
        """GET url -> (json|None, meta). meta: status, elapsed, attempts, n503, n429."""
        meta = {"status": None, "elapsed": None, "attempts": 0, "n503": 0, "n429": 0}
        backoff = 2.0
        for attempt in range(1, MAX_RETRIES + 1):
            self._wait()
            self.requests += 1
            meta["attempts"] = attempt
            t0 = time.monotonic()
            status, body, retry_after = None, b"", None
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    status, body = resp.status, resp.read()
            except urllib.error.HTTPError as e:
                status = e.code
                retry_after = e.headers.get("Retry-After")
                try:
                    body = e.read()
                except Exception:
                    body = b""
            except Exception as e:  # network / timeout
                meta["elapsed"] = time.monotonic() - t0
                self.status_counts["net-error"] += 1
                self.errors.append(f"{label or url}: attempt {attempt} network error {type(e).__name__}: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            meta["elapsed"] = time.monotonic() - t0
            meta["status"] = status
            self.status_counts[str(status)] += 1
            if status == 503:
                meta["n503"] += 1
            if status == 429:
                meta["n429"] += 1
            # 404 on product lookup is a legit "not found" answer; try to parse its JSON
            if status == 200 or status == 404:
                try:
                    return json.loads(body.decode("utf-8")), meta
                except Exception:
                    # OFF sometimes serves an HTML "temporarily unavailable" page with HTTP 200
                    self.errors.append(f"{label or url}: attempt {attempt} HTTP {status} but non-JSON body "
                                       f"({body[:60]!r}) - retrying")
                    meta["n503"] += 1 if b"unavailable" in body.lower() else 0
            else:
                self.errors.append(f"{label or url}: attempt {attempt} HTTP {status}")
            wait = backoff
            if retry_after and str(retry_after).isdigit():
                wait = max(wait, int(retry_after))
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
        self.errors.append(f"{label or url}: GAVE UP after {MAX_RETRIES} attempts")
        return None, meta


# ----------------------------------------------------------------------------- normalisation
def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x)
    return str(v).strip()


def normalise(raw: dict, db: str, match: str) -> dict:
    cats_tags = raw.get("categories_tags") or []
    if isinstance(cats_tags, str):
        cats_tags = [cats_tags]
    cats_text = _s(raw.get("categories"))
    image = _s(raw.get("image_front_url")) or _s(raw.get("image_url"))
    if not image and isinstance(raw.get("images"), dict) and raw["images"]:
        image = "(has images)"  # Search-a-licious returns an images dict, not a URL
    return {
        "barcode": _s(raw.get("code")),
        "name": _s(raw.get("product_name")),
        "brand": _s(raw.get("brands")),
        "quantity": _s(raw.get("quantity")),
        "categories": cats_text or ", ".join(t.split(":", 1)[-1] for t in cats_tags),
        "cat_tags": [t.split(":", 1)[-1] for t in cats_tags],
        "image_url": image,
        "countries": _s(raw.get("countries_tags")),
        "db": db,
        "match": match,
    }


# ----------------------------------------------------------------------------- discovery
def fetch_country(http: Http, db: str, max_pages: int) -> tuple[int, dict[str, dict], bool]:
    """Page through v2 search countries_tags=en:pakistan. Returns (reported_count, items, complete)."""
    items: dict[str, dict] = {}
    total, page, complete = 0, 1, True
    while True:
        q = urllib.parse.urlencode({"countries_tags": "en:pakistan", "page_size": PAGE_SIZE,
                                    "page": page, "fields": FIELDS})
        data, _ = http.get_json(f"{DBS[db]}/api/v2/search?{q}", f"{db} country p{page}")
        if data is None:
            complete = False
            break
        total = int(data.get("count") or 0)
        for p in data.get("products") or []:
            it = normalise(p, db, "country")
            if it["barcode"]:
                items.setdefault(it["barcode"], it)
        pages = int(data.get("page_count") or 0)
        print(f"  [{db}] country page {page}/{pages}  got {len(items)}/{total}", flush=True)
        if page >= pages or page >= max_pages:
            complete = page >= pages
            break
        page += 1
    return total, items, complete


def fetch_prefix_sal(http: Http, max_pages: int) -> tuple[int, dict[str, dict], bool]:
    """Food only: Search-a-licious wildcard query code:896*."""
    items: dict[str, dict] = {}
    total, page, complete = 0, 1, True
    fields = "code,product_name,brands,quantity,categories_tags,images,countries_tags"
    while True:
        q = urllib.parse.urlencode({"q": "code:896*", "page_size": PAGE_SIZE, "page": page, "fields": fields})
        data, _ = http.get_json(f"{SAL_URL}?{q}", f"off prefix-896 p{page}")
        if data is None:
            complete = False
            break
        total = int(data.get("count") or 0)
        for p in data.get("hits") or []:
            it = normalise(p, "off", "prefix896")
            if it["barcode"]:
                items.setdefault(it["barcode"], it)
        pages = int(data.get("page_count") or 0)
        print(f"  [off] prefix-896 page {page}/{pages}  got {len(items)}/{total}", flush=True)
        if page >= pages or page >= max_pages:
            complete = page >= pages
            break
        page += 1
    return total, items, complete


# ----------------------------------------------------------------------------- stats
def stats(items: list[dict]) -> dict:
    n = len(items)
    def cnt(key):
        return sum(1 for i in items if i[key])
    brands: Counter[str] = Counter()
    cats: Counter[str] = Counter()
    for i in items:
        for b in i["brand"].split(","):
            b = b.strip().lower()
            if b:
                brands[b] += 1
        for c in i["cat_tags"]:
            cats[c] += 1
    return {
        "total": n, "name": cnt("name"), "brand": cnt("brand"), "quantity": cnt("quantity"),
        "categories": cnt("categories"), "image": cnt("image_url"),
        "top_brands": brands.most_common(30), "top_cats": cats.most_common(20),
    }


def pct(a: int, b: int) -> str:
    return f"{(100.0 * a / b):.1f}%" if b else "n/a"


# ----------------------------------------------------------------------------- report
def md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")[:80]


def stats_table(title: str, st: dict) -> list[str]:
    t = st["total"]
    return [
        f"**{title}** - {t} items",
        "",
        "| Field | Count | % |", "|---|---:|---:|",
        f"| product_name | {st['name']} | {pct(st['name'], t)} |",
        f"| brand | {st['brand']} | {pct(st['brand'], t)} |",
        f"| quantity / pack size | {st['quantity']} | {pct(st['quantity'], t)} |",
        f"| categories | {st['categories']} | {pct(st['categories'], t)} |",
        f"| >=1 image | {st['image']} | {pct(st['image'], t)} |",
        "",
    ]


def build_report(ctx: dict) -> str:
    L: list[str] = []
    L += ["# Open Food Facts family - Pakistani product coverage probe", "",
          f"Generated: {ctx['now']}  ", f"User-Agent: `{USER_AGENT}`  ",
          f"HTTP requests made: {ctx['http'].requests} (<= 1/sec, backoff on 429/503)  ",
          f"Run time: {ctx['elapsed']:.0f}s", "",
          "## License / attribution", "",
          "- Open Food Facts / Open Beauty Facts / Open Products Facts **database**: Open Database License "
          "(**ODbL 1.0**) - attribution + share-alike for derived databases.",
          "- Individual database **contents** (data): Database Contents License (DbCL).",
          "- Product **images**: **CC BY-SA 3.0** (some are CC BY-SA 4.0) - attribute and share alike; "
          "do not assume they can be bundled into a proprietary catalog without compliance.",
          "- Required credit if used: \"Data from Open Food Facts / Open Beauty Facts / Open Products Facts "
          "(openfoodfacts.org), ODbL; images CC BY-SA.\"",
          "- Implication for PakPOS: a read-only *lookup fallback* (query at runtime, show the source) is the "
          "lowest-risk use. Copying the data into our own DB makes it a derived database subject to "
          "share-alike on that dataset.", ""]

    L += ["## Totals per database", "",
          "| DB | v2 country=Pakistan (reported count) | fetched | complete? | 896-prefix (in fetched set) | "
          "896-prefix via Search-a-licious (food only) |", "|---|---:|---:|:--:|---:|---:|"]
    for db in DBS:
        r = ctx["res"][db]
        sal = f"{r['sal_total']} (fetched {len(r['sal'])})" if db == "off" else "n/a"
        L.append(f"| {DB_NAMES[db]} | {r['country_total']} | {len(r['country'])} | "
                 f"{'yes' if r['country_complete'] else 'NO (capped/errors)'} | {r['country_896']} | {sal} |")
    L.append("")

    L += ["## Field completeness", ""]
    for db in DBS:
        r = ctx["res"][db]
        L += [f"### {DB_NAMES[db]}", ""]
        L += stats_table("A. countries_tags = pakistan", stats(list(r["country"].values())))
        L += stats_table("B. barcode starts with 896 (union of country-set 896 + prefix query)",
                         stats(list(r["prefix"].values())))
        L += stats_table("A ∪ B (distinct)", stats(list(r["union"].values())))

    L += ["### All databases combined (distinct barcodes, A ∪ B)", ""]
    L += stats_table("combined", ctx["combined_stats"])

    L += ["## Top 30 brands (combined A ∪ B)", "", "| # | Brand | Items |", "|--:|---|--:|"]
    for i, (b, c) in enumerate(ctx["combined_stats"]["top_brands"], 1):
        L.append(f"| {i} | {md_escape(b)} | {c} |")
    L += ["", "## Top 20 categories (combined A ∪ B)", "", "| # | Category tag | Items |", "|--:|---|--:|"]
    for i, (b, c) in enumerate(ctx["combined_stats"]["top_cats"], 1):
        L.append(f"| {i} | {md_escape(b)} | {c} |")
    L.append("")
    for db in DBS:
        st = stats(list(ctx["res"][db]["union"].values()))
        if st["total"] and db != "off":
            L += [f"<details><summary>{DB_NAMES[db]}: top brands / categories</summary>", "",
                  "Brands: " + "; ".join(f"{b} ({c})" for b, c in st["top_brands"]), "",
                  "Categories: " + "; ".join(f"{b} ({c})" for b, c in st["top_cats"]), "", "</details>", ""]

    lk = ctx["lookups"]
    L += ["## Lookup path test: /api/v2/product/{barcode}", "",
          f"Sampled {len(lk)} random 896-prefix barcodes (seed {ctx['seed']}).", ""]
    if lk:
        ok = [x for x in lk if x["found"]]
        times = sorted(x["elapsed"] for x in lk if x["elapsed"] is not None)
        n503 = sum(x["n503"] for x in lk)
        n429 = sum(x["n429"] for x in lk)
        L += [f"- found: **{len(ok)}/{len(lk)}** ({pct(len(ok), len(lk))})",
              f"- with a product_name: {sum(1 for x in lk if x['name'])}/{len(lk)}",
              f"- 503 responses encountered (incl. retried): **{n503}**, 429: {n429}",
              f"- failed after retries: {sum(1 for x in lk if x['status'] not in (200, 404))}",
              ]
        if times:
            L.append(f"- response time: min {times[0]:.2f}s / median {times[len(times)//2]:.2f}s / "
                     f"p90 {times[int(len(times)*0.9)-1 if len(times) > 1 else 0]:.2f}s / max {times[-1]:.2f}s")
        L += ["", "| Barcode | DB | HTTP | found | time (s) | attempts | 503s | name |",
              "|---|---|--:|:--:|--:|--:|--:|---|"]
        for x in lk:
            L.append(f"| {x['barcode']} | {x['db']} | {x['status']} | {'yes' if x['found'] else 'no'} | "
                     f"{(x['elapsed'] or 0):.2f} | {x['attempts']} | {x['n503']} | {md_escape(x['name'])} |")
    L.append("")

    L += ["## 20 sample rows", "", "| Barcode | Name | Brand | Quantity | Categories | Image | DB | Match |",
          "|---|---|---|---|---|:--:|---|---|"]
    for it in ctx["samples"]:
        L.append(f"| {it['barcode']} | {md_escape(it['name'])} | {md_escape(it['brand'])} | "
                 f"{md_escape(it['quantity'])} | {md_escape(it['categories'])[:50]} | "
                 f"{'yes' if it['image_url'] else 'no'} | {it['db']} | {it['match']} |")
    L.append("")

    L += ["## Errors / retries", ""]
    http = ctx["http"]
    L.append("HTTP status counts: " + ", ".join(f"{k}: {v}" for k, v in sorted(http.status_counts.items())))
    L.append("")
    if http.errors:
        L.append(f"{len(http.errors)} error events (first 60):")
        L.append("")
        for e in http.errors[:60]:
            L.append(f"- {md_escape(e)}")
    else:
        L.append("No errors.")
    L += ["", "## Conclusion", "", ctx["conclusion"], ""]
    return "\n".join(L)


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=250, help="page cap per query (100 items/page)")
    ap.add_argument("--sample", type=int, default=40, help="barcodes for the lookup test")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    t_start = time.monotonic()
    http = Http()
    res: dict[str, dict] = {}

    for db in DBS:
        print(f"== {DB_NAMES[db]}: country=pakistan", flush=True)
        c_total, c_items, c_complete = fetch_country(http, db, args.max_pages)
        sal_total, sal_items = 0, {}
        if db == "off":
            print("== Open Food Facts: prefix 896* (Search-a-licious)", flush=True)
            sal_total, sal_items, _ = fetch_prefix_sal(http, args.max_pages)
        country_896 = {k: v for k, v in c_items.items() if k.startswith("896")}
        prefix = dict(country_896)
        for k, v in sal_items.items():
            if k in c_items:
                continue
            prefix.setdefault(k, v)
        union = dict(c_items)
        for k, v in prefix.items():
            union.setdefault(k, v)
        res[db] = {"country_total": c_total, "country": c_items, "country_complete": c_complete,
                   "country_896": len(country_896), "sal_total": sal_total, "sal": sal_items,
                   "prefix": prefix, "union": union}

    # combined distinct (barcode -> best record)
    combined: dict[str, dict] = {}
    for db in DBS:
        for k, v in res[db]["union"].items():
            cur = combined.get(k)
            if cur is None or (not cur["name"] and v["name"]):
                combined[k] = v
    combined_list = list(combined.values())
    combined_stats = stats(combined_list)

    # ---- lookup test on random 896 barcodes
    rng = random.Random(args.seed)
    pool = [(v["barcode"], v["db"]) for db in DBS for v in res[db]["prefix"].values()]
    pool = sorted(set(pool))
    sample = rng.sample(pool, min(args.sample, len(pool)))
    print(f"== lookup test: {len(sample)} barcodes", flush=True)
    lookups = []
    for i, (code, db) in enumerate(sample, 1):
        q = urllib.parse.urlencode({"fields": FIELDS})
        data, meta = http.get_json(f"{DBS[db]}/api/v2/product/{code}.json?{q}", f"lookup {db} {code}")
        found = bool(data and data.get("status") == 1 and data.get("product"))
        name = _s((data or {}).get("product", {}).get("product_name")) if found else ""
        lookups.append({"barcode": code, "db": db, "found": found, "name": name, **meta})
        print(f"  {i}/{len(sample)} {code} {db} HTTP {meta['status']} found={found} "
              f"{(meta['elapsed'] or 0):.2f}s 503s={meta['n503']}", flush=True)

    # ---- samples: 20 random distinct items that have a name
    named = sorted((x for x in combined_list if x["name"]), key=lambda x: x["barcode"])
    samples = random.Random(args.seed).sample(named, min(20, len(named)))

    # ---- conclusion
    n_named = combined_stats["name"]
    n_total = combined_stats["total"]
    n_complete = sum(1 for x in combined_list if x["name"] and x["brand"] and x["quantity"])
    ok = sum(1 for x in lookups if x["found"])
    hit = pct(ok, len(lookups))
    n896 = len({k for db in DBS for k in res[db]["prefix"]})
    n896_named = len({k for db in DBS for k, v in res[db]["prefix"].items() if v["name"]})
    verdict = ("USABLE as a *best-effort fallback / suggestion source* (not as the primary catalog)"
               if n_named >= 1500 else
               "MARGINAL - only a small long-tail fallback; not worth heavy integration" if n_named >= 300
               else "NOT worth it - too sparse")
    conclusion = (
        f"- Distinct Pakistani items found (country tag OR 896 prefix, all 3 DBs): **{n_total}**, of which "
        f"**{n_named} have a product name** ({pct(n_named, n_total)}); {n_complete} have name+brand+pack size.\n"
        f"- 896-prefix items: {n896} ({n896_named} named). Country-tagged items often carry non-896 barcodes "
        f"(imports sold in Pakistan), so 896 alone under-counts local brands in other DBs.\n"
        f"- Lookup path `/api/v2/product/{{barcode}}`: {ok}/{len(lookups)} found ({hit}); "
        f"{sum(x['n503'] for x in lookups)} 503s seen during lookups, {http.status_counts.get('503', 0)} in total. "
        f"The public OFF servers do return transient 503 / HTML 'temporarily unavailable' pages, so any "
        f"integration needs timeouts, retries and a cache.\n"
        f"- Verdict: {verdict}. Names are present for most items but pack size / category / image coverage "
        f"is partial (see tables). Pakistan has ~1M+ SKUs in retail; a few thousand entries is a small "
        f"fraction, so expect a low hit rate on a random shop barcode - useful as autofill, not as truth.\n"
        f"- Licensing: ODbL/CC BY-SA attribution + share-alike (see top of report)."
    )

    ctx = {"now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "http": http,
           "elapsed": time.monotonic() - t_start, "res": res, "combined_stats": combined_stats,
           "lookups": lookups, "samples": samples, "seed": args.seed, "conclusion": conclusion}

    if not args.no_write:
        DOCS.mkdir(exist_ok=True)
        (DOCS / "off_pakistan_probe_report.md").write_text(build_report(ctx), encoding="utf-8")
        with (DOCS / "off_pakistan_items.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["barcode", "name", "brand", "quantity", "categories", "image_url", "source_db", "match"])
            for db in DBS:
                for it in sorted(res[db]["union"].values(), key=lambda x: x["barcode"]):
                    w.writerow([it["barcode"], it["name"], it["brand"], it["quantity"], it["categories"],
                                it["image_url"], db, it["match"]])
        print(f"\nWrote {DOCS / 'off_pakistan_probe_report.md'} and {DOCS / 'off_pakistan_items.csv'}")

    print("\n=== SUMMARY ===")
    for db in DBS:
        r = res[db]
        print(f"{DB_NAMES[db]:22s} country-tag reported={r['country_total']:6d} fetched={len(r['country']):6d} "
              f"896-prefix={len(r['prefix']):5d}")
    print(f"Combined distinct: {n_total}  named: {n_named}  name+brand+qty: {n_complete}")
    print(f"Lookup: {ok}/{len(lookups)} found, 503s={sum(x['n503'] for x in lookups)}")
    print("\nCONCLUSION\n" + conclusion)
    return 0


if __name__ == "__main__":
    sys.exit(main())
