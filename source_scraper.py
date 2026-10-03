#!/usr/bin/env python3
"""Scrape the extra URLs saved for one category (admin page → Sources).

Supported URLs
  Myntra product   https://www.myntra.com/shirts/brand/name/12345678/buy  (or myntra.com/12345678)
  Myntra listing   any other myntra.com page (search, brand, filtered list) — every product on it
  Shopify store    https://store.com, /collections/<name>, or /products/<handle>

Myntra products go through the normal product-ID scraper, so they are identical to brand-scraped
ones. Shopify's JSON feed has price, MRP and per-size availability but no stock. Some stores publish
per-size stock in the product page source (theme or inventory-app JSON keyed by variant id); when it
is there, those sizes are stored as real stock and sales are measured from it like on Myntra.
Otherwise sizes are stored as 1 unit when available and tagged 'availability_only', and the sales
engine skips those products (see Database._fully_shared_product_ids) instead of inventing sales.

Run from inside a category folder (so that category's config/database are used):
    cd "<Category>" && python3 ../source_scraper.py [--snapshot]
The category's main.py also calls run_category_sources() before its own snapshot.
"""

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

SOURCES_FILE = "sources.json"
MYNTRA_HOSTS = ("myntra.com", "www.myntra.com")
SHOPIFY_PAGE_SIZE = 250
PAGE_STOCK_PROBE = 3                # stop reading product pages for a store after this many without stock
PAGE_DELAY_SECONDS = 0.7            # pause between product page reads
MAX_PAGES = 40                      # 40 x 250 Shopify products, 40 x 50 Myntra listing items
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


def load_sources(folder: Path) -> list:
    try:
        data = json.loads((folder / SOURCES_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in data.get("urls", []) if isinstance(s, dict) and s.get("url")]


def classify(url: str):
    """Return (kind, detail): myntra_product(id) | myntra_listing(url) | shopify(url) | invalid(reason)."""
    parts = urlparse(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "invalid", "not an http(s) URL"
    host = parts.netloc.lower()
    if host in MYNTRA_HOSTS or host.endswith(".myntra.com"):
        m = re.search(r"/(\d{5,})(?:/buy)?/?$", parts.path)
        if m:
            return "myntra_product", int(m.group(1))
        return "myntra_listing", url.strip()
    return "shopify", url.strip()


# ── Shopify ────────────────────────────────────────────────────────────────────────────────
def _shopify_endpoints(url: str):
    """The JSON feed(s) behind a Shopify URL, as (kind, endpoint)."""
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    m = re.search(r"/products/([^/?#]+)", parts.path)
    if m:
        return base, "product", f"{base}/products/{m.group(1)}.json"
    m = re.search(r"/collections/([^/?#]+)", parts.path)
    if m:
        return base, "list", f"{base}/collections/{m.group(1)}/products.json"
    return base, "list", f"{base}/products.json"


def fetch_shopify_products(url: str, session: requests.Session) -> tuple:
    base, kind, endpoint = _shopify_endpoints(url)
    if kind == "product":
        r = session.get(endpoint, timeout=30)
        r.raise_for_status()
        product = (r.json() or {}).get("product")
        if not product:
            raise ValueError("no product in Shopify response")
        # The per-product .json feed omits variant `available` on some stores; the storefront .js
        # endpoint carries it, so merge it in by variant id (the list feeds already include it).
        if any("available" not in v for v in product.get("variants") or []):
            try:
                js = session.get(endpoint[:-len(".json")] + ".js", timeout=30).json()
                avail = {int(v["id"]): bool(v.get("available")) for v in js.get("variants") or [] if v.get("id")}
                for v in product.get("variants") or []:
                    if "available" not in v and int(v["id"]) in avail:
                        v["available"] = avail[int(v["id"])]
            except (requests.RequestException, ValueError):
                pass
        return base, [product]
    products = []
    for page in range(1, MAX_PAGES + 1):
        r = session.get(endpoint, params={"limit": SHOPIFY_PAGE_SIZE, "page": page}, timeout=30)
        if r.status_code == 404 and page == 1:
            raise ValueError("not a Shopify store (no products.json feed)")
        r.raise_for_status()
        try:
            batch = r.json().get("products") or []
        except ValueError:
            raise ValueError("not a Shopify store (feed is not JSON)")
        if not batch:
            break
        products.extend(batch)
        if len(batch) < SHOPIFY_PAGE_SIZE:
            break
        time.sleep(1.0)  # be polite to the store
    return base, products


def extract_page_inventory(html_text: str, variant_ids) -> dict:
    """Per-variant stock published in a Shopify product page's source, keyed by variant id.

    Reads only what the page already contains (formats from the SHOPIFY_SCRAP project's tiers 1-4):
      window.inventories['<pid>'][<vid>] = {'quantity': N}              (theme JS)
      GloboPreorderParams: variants[i] = {...id...}; variants[i].inventory_quantity = N
      window.gwProductInventoryQuantity[<vid>] = "N"                     (GrowWave)
      <vid> : {"inventory_policy":"deny", "inventory_quantity": N}       (inventory-app maps)
      {"id": <vid>, ..., "inventory_quantity": N}                        (theme JSON, one object)
    Only this product's variant ids are accepted, so another product's numbers can never leak in."""
    ids = {int(v) for v in variant_ids}
    found = {}
    for vid, qty in re.findall(r"window\.inventories\[['\"]\d+['\"]\]\[(\d+)\]\s*=\s*\{\s*['\"]quantity['\"]\s*:\s*(-?\d+)", html_text):
        if int(vid) in ids:
            found.setdefault(int(vid), int(qty))
    if "GloboPreorderParams" in html_text:
        qty_by_index = {int(i): int(q) for i, q in re.findall(r"variants\[(\d+)\]\.inventory_quantity\s*=\s*(-?\d+);", html_text)}
        for idx, body in re.findall(r"variants\[(\d+)\]\s*=\s*(\{.*?\});", html_text):
            m = re.search(r'"id"\s*:\s*(\d+)', body)
            if m and int(m.group(1)) in ids and int(idx) in qty_by_index:
                found.setdefault(int(m.group(1)), qty_by_index[int(idx)])
    for vid, qty in re.findall(r'window\.gwProductInventoryQuantity\[(\d+)\]\s*=\s*"?(-?\d+)"?\s*;', html_text):
        if int(vid) in ids:
            found.setdefault(int(vid), int(qty))
    for m in re.finditer(r'(\d{8,})\s*:\s*\{[^{}]*?"inventory_?[qQ]uantity"\s*:\s*(-?\d+)', html_text):
        vid = int(m.group(1))
        if vid in ids:
            found.setdefault(vid, int(m.group(2)))
    for vid in ids - set(found):
        m = (re.search(rf'"id"\s*:\s*{vid}\b[^{{}}]*?"inventory_?[qQ]uantity"\s*:\s*(-?\d+)', html_text)
             or re.search(rf'"inventory_?[qQ]uantity"\s*:\s*(-?\d+)[^{{}}]*?"id"\s*:\s*{vid}\b', html_text))
        if m:
            found[vid] = int(m.group(1))
    # Numbers are only stock when Shopify tracks that variant. Untracked variants
    # (inventory_management: null) carry junk such as 0 / -1 / -6, and real stock is never negative.
    untracked = untracked_variant_ids(html_text, ids)
    return {vid: q for vid, q in found.items() if vid not in untracked and q >= 0}


def untracked_variant_ids(html_text: str, variant_ids) -> set:
    """Variants whose page JSON says inventory_management / inventoryManagement is null."""
    out = set()
    for vid in {int(v) for v in variant_ids}:
        if (re.search(rf'"id"\s*:\s*{vid}\b[^{{}}]*?"inventory_?[mM]anagement"\s*:\s*null', html_text)
                or re.search(rf'"inventory_?[mM]anagement"\s*:\s*null[^{{}}]*?"id"\s*:\s*{vid}\b', html_text)):
            out.add(vid)
    return out


_SHIRT = re.compile(r"shirt", re.I)
_NOT_SHIRT = re.compile(r"\bt[\s-]?shirts?\b|\btees?\b|\bsweat[\s-]?shirts?\b|\bpolos?\b|\bhoodies?\b|\bjeans?\b"
                        r"|\btrousers?\b|\bpants?\b|\bshorts\b|\bjoggers?\b", re.I)


def is_shirt(product: dict) -> bool:
    """Shirt by title or product type, and not a T-shirt/tee/polo/bottom (word-bounded, so "Velvet Shirt" stays)."""
    text = f"{product.get('title') or ''} {product.get('product_type') or ''}"
    return bool(_SHIRT.search(text)) and not _NOT_SHIRT.search(product.get("title") or "")


def _size_option_index(product: dict) -> int:
    for i, opt in enumerate(product.get("options") or []):
        if str(opt.get("name") or "").strip().lower() in ("size", "sizes"):
            return i
    return 0


def shopify_to_schema(product: dict, base: str, stock: dict = None) -> dict:
    """Map a Shopify product to the dict Database.save_product expects.
    `stock` (variant id -> units from the page source) turns availability into real counts."""
    variants = product.get("variants") or []
    size_idx = _size_option_index(product)
    priced = [v for v in variants if v.get("price")]
    cheapest = min(priced, key=lambda v: float(v["price"])) if priced else {}
    price = float(cheapest.get("price") or 0)
    compare = float(cheapest.get("compare_at_price") or 0)
    mrp = compare if compare > price else price
    sizes = []
    for v in variants:
        size = v.get(f"option{size_idx + 1}") or v.get("title") or "One Size"
        if "available" in v:
            available = bool(v.get("available"))
        else:  # feed gave no availability: trust published stock, else assume listed = in stock
            available = (stock[v["id"]] > 0) if stock and v.get("id") in stock else True
        if stock and v.get("id") in stock:
            count = max(int(stock[v["id"]]), 0) if available else 0
        else:
            count = 1 if available else 0
        sizes.append({"size": str(size), "sku_id": v.get("id"), "available": available, "inventory_count": count})
    images = product.get("images") or []
    text = re.sub(r"<[^>]+>", " ", html.unescape(product.get("body_html") or ""))
    return {
        "product_info": {
            "product_id": int(product["id"]),
            "sku": str(cheapest.get("sku") or ""),
            "brand": product.get("vendor") or urlparse(base).netloc,
            "brand_type": "Shopify D2C",
            "is_myntra_label": False,
            "title": product.get("title") or "",
            "category": product.get("product_type") or "",
            "sub_category": product.get("product_type") or "",
            "product_url": f"{base}/products/{product.get('handle')}",
        },
        "pricing": {"selling_price": price, "mrp": mrp,
                    "discount_percentage": round((mrp - price) * 100 / mrp) if mrp > price else 0},
        "inventory_and_sizes": {"is_in_stock": any(s["available"] for s in sizes), "sizes_available": sizes},
        "media": {"primary_image": (images[0] or {}).get("src", "") if images else ""},
        "specifications": {"tags": product.get("tags")},
        "description": re.sub(r"\s+", " ", text).strip()[:2000],
        "source": {"type": "shopify", "store": base, "stock": "page_source" if stock else "availability_only",
                   "fetched_at": datetime.now().astimezone().isoformat()},
    }


# ── Exact-inventory files from the SHOPIFY_SCRAP project ───────────────────────────────────
def _env_value(key: str, folder: Path) -> str:
    import os
    if os.environ.get(key):
        return os.environ[key].strip()
    for env_file in (folder.parent / ".env", folder / ".env"):
        try:
            for line in env_file.read_text("utf-8").splitlines():
                name, sep, value = line.partition("=")
                if sep and name.strip() == key:
                    return value.strip().strip("'\"")
        except OSError:
            continue
    return ""


def load_imported_inventory(base: str, folder: Path) -> tuple:
    """Variant id -> exact stock from `<store>_exact_inventory.csv` in SHOPIFY_INVENTORY_DIR (.env).

    Those files are produced by the separate SHOPIFY_SCRAP tool, which you run yourself; this only
    reads them. A file older than SHOPIFY_INVENTORY_MAX_AGE_HOURS (default 24) is ignored as stale."""
    import csv
    directory = _env_value("SHOPIFY_INVENTORY_DIR", folder)
    if not directory:
        return {}, None
    host = urlparse(base).netloc.lower()
    slug = re.sub(r"^www\.", "", host).split(".")[0]
    path = Path(directory).expanduser() / f"{slug}_exact_inventory.csv"
    try:
        max_age = float(_env_value("SHOPIFY_INVENTORY_MAX_AGE_HOURS", folder) or 24)
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    except (OSError, ValueError):
        return {}, None
    if age_hours > max_age:
        return {}, f"{path.name} is {age_hours:.0f}h old (limit {max_age:.0f}h) — ignored"
    stock = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                stock[int(row["variant_id"])] = int(float(row["exact_quantity_left"]))
            except (KeyError, TypeError, ValueError):
                continue
    return stock, f"{path.name} ({age_hours:.1f}h old, {len(stock)} variants)"


# ── Myntra ─────────────────────────────────────────────────────────────────────────────────
def myntra_listing_ids(scraper, url: str) -> list:
    ids, context = [], ""
    for page in range(1, MAX_PAGES + 1):
        results, context = scraper._fetch_gateway_listing_page(url, page, context or "")
        batch = [int(p["productId"]) for p in (results.get("products") or []) if p.get("productId")]
        if not batch:
            break
        ids.extend(i for i in batch if i not in ids)
        total = results.get("totalCount") or 0
        if total and len(ids) >= int(total):
            break
    return ids


# ── Runner ─────────────────────────────────────────────────────────────────────────────────
def run_category_sources(folder: Path = None, take_snapshot: bool = False) -> dict:
    """Scrape every saved URL for the category in `folder` (default: current directory)."""
    folder = Path(folder or Path.cwd()).resolve()
    sources = load_sources(folder)
    summary = {"category": folder.name, "sources": len(sources), "results": [],
               "myntra_saved": 0, "shopify_saved": 0, "errors": 0}
    if not sources:
        return summary

    from database import Database
    db = Database()
    db.enforce_price_band = False   # pasted URLs are kept whatever their price
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    myntra_ids, myntra_scraper = [], None
    # Per category: SOURCES_SHIRTS_ONLY=1 in its .env keeps only shirts from pasted Shopify links.
    shirts_only = _env_value("SOURCES_SHIRTS_ONLY", folder).lower() in {"1", "true", "yes", "on"}

    for src in sources:
        url = src["url"]
        kind, detail = classify(url)
        result = {"url": url, "kind": kind}
        try:
            if kind == "invalid":
                raise ValueError(detail)
            if kind == "myntra_product":
                myntra_ids.append(detail)
                result["products"] = 1
            elif kind == "myntra_listing":
                if myntra_scraper is None:
                    from scraper import MyntraEthnicScraper
                    myntra_scraper = MyntraEthnicScraper(brands=[], price_min=0, price_max=0)
                    myntra_scraper.db.enforce_price_band = False
                found = myntra_listing_ids(myntra_scraper, detail)
                myntra_ids.extend(i for i in found if i not in myntra_ids)
                result["products"] = len(found)
            else:
                base, products = fetch_shopify_products(detail, session)
                if shirts_only:
                    kept = [p for p in products if is_shirt(p)]
                    if len(kept) < len(products):
                        result["skipped_not_shirts"] = len(products) - len(kept)
                    products = kept
                imported, imported_note = load_imported_inventory(base, folder)
                if imported_note:
                    result["imported_file"] = imported_note
                saved_ids, availability_only, misses, untracked_seen = [], [], 0, 0
                for p in products:
                    stock = {}
                    if misses < PAGE_STOCK_PROBE and p.get("handle"):
                        try:
                            page = session.get(f"{base}/products/{p['handle']}", timeout=30,
                                               headers={"Accept": "text/html"})
                            if page.ok:
                                vids = [v["id"] for v in p.get("variants") or []]
                                stock = extract_page_inventory(page.text, vids)
                                untracked_seen += len(untracked_variant_ids(page.text, vids))
                        except requests.RequestException:
                            stock = {}
                        misses = 0 if stock else misses + 1
                        time.sleep(PAGE_DELAY_SECONDS)
                    # Sizes the page did not publish: fill from a fresh SHOPIFY_SCRAP export, if any.
                    for v in p.get("variants") or []:
                        if v.get("id") not in stock and v.get("id") in imported:
                            stock[v["id"]] = imported[v["id"]]
                    if db.save_product(shopify_to_schema(p, base, stock)):
                        saved_ids.append(int(p["id"]))
                        if not stock:
                            availability_only.append(int(p["id"]))
                if availability_only:
                    cur = db._get_connection().cursor()
                    cur.execute("UPDATE product_sizes SET inventory_quality = 'availability_only' "
                                "WHERE product_id = ANY(?);", (availability_only,))
                result["with_stock"] = len(saved_ids) - len(availability_only)
                if availability_only and not result["with_stock"]:
                    result["note"] = ("store does not track stock (inventory_management is null) — availability only"
                                      if untracked_seen else
                                      "stock is tracked but not published in the page — availability only"
                                      + (" (no fresh SHOPIFY_SCRAP file)" if not imported else ""))
                summary["shopify_saved"] += len(saved_ids)
                result["products"] = len(saved_ids)
                result["fetched"] = len(products)
                if not products:
                    raise ValueError("no products at this URL (empty collection, or wrong collection name)")
                if len(saved_ids) < len(products):
                    result["note"] = f"{len(products) - len(saved_ids)} of {len(products)} could not be saved"
            result["status"] = "ok"
        except Exception as exc:
            summary["errors"] += 1
            result.update(status="error", error=str(exc)[:300])
        summary["results"].append(result)
        print(f"[sources] {kind:15s} {result.get('status'):5s} saved {result.get('products', 0):5}"
              + (f" of {result['fetched']}" if 'fetched' in result else "")
              + (f" (stock counts for {result['with_stock']})" if 'with_stock' in result else "")
              + (f" [skipped {result['skipped_not_shirts']} non-shirts]" if result.get('skipped_not_shirts') else "") + f"  {url}"
              + (f"  — {result.get('error') or result.get('note')}" if result.get('error') or result.get('note') else "")
              + (f"  [stock file: {result['imported_file']}]" if result.get('imported_file') else ""), flush=True)

    if myntra_ids:
        if myntra_scraper is None:
            from scraper import MyntraEthnicScraper
            myntra_scraper = MyntraEthnicScraper(brands=[], price_min=0, price_max=0)
            myntra_scraper.db.enforce_price_band = False
        r = myntra_scraper.scrape_product_ids(myntra_ids, take_snapshot=False)
        summary["myntra_saved"] = int((r or {}).get("successful_count") or 0)
        summary["myntra_failed"] = int((r or {}).get("failed_count") or 0)

    if take_snapshot and (summary["myntra_saved"] or summary["shopify_saved"]):
        summary["snapshot"] = db.take_daily_snapshot()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--snapshot", action="store_true",
                        help="Take a snapshot afterwards (the category's normal scrape takes one anyway).")
    args = parser.parse_args()
    sys.path.insert(0, str(Path.cwd()))
    summary = run_category_sources(take_snapshot=args.snapshot)
    print(json.dumps(summary, indent=2, default=str))
    return 1 if summary["errors"] and not (summary["myntra_saved"] or summary["shopify_saved"]) else 0


if __name__ == "__main__":
    sys.exit(main())
