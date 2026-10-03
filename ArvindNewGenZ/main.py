#!/usr/bin/env python3
"""Daily Shirts Myntra scraper for the Arvind New GenZ brands — ₹700–₹3000 price segment."""

import argparse
import json
import sys

from config import DEFAULT_WORKERS, POLITE_DELAY
from database import Database
from scraper import MyntraEthnicScraper
from scraper_logging import configure_scraper_logging, generate_run_id

# Brands live in one place: TARGET_SHIRTS_BRANDS in shirts_taxonomy.py (₹700–₹3000 segment).
from shirts_taxonomy import TARGET_SHIRTS_BRANDS

from config import PRICE_MIN, PRICE_MAX  # one definition of the ₹ band


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Arvind New GenZ shirt brands (₹700–₹3000 price segment) from Myntra."
    )
    parser.add_argument(
        "--brands",
        type=str,
        default="",
        help="Optional comma-separated subset of brands to run instead of the full brand allowlist.",
    )
    parser.add_argument(
        "--price-min",
        type=int,
        default=PRICE_MIN,
        help=f"Minimum price filter (default: ₹{PRICE_MIN}).",
    )
    parser.add_argument(
        "--price-max",
        type=int,
        default=PRICE_MAX,
        help=f"Maximum price filter (default: ₹{PRICE_MAX}).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel PDP fetch workers (default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=POLITE_DELAY,
        help=f"Delay between listing page requests in seconds (default: {POLITE_DELAY}).",
    )
    parser.add_argument(
        "--max-pages-per-brand",
        type=int,
        default=0,
        help="Limit listing pages per brand for testing (0 = all pages).",
    )
    parser.add_argument(
        "--limit-products",
        type=int,
        default=0,
        help="Global safety cap for kept products in the current run (0 = unlimited).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate without saving products to PostgreSQL.",
    )
    parser.add_argument(
        "--pdp-retry-rounds",
        type=int,
        default=5,
        help="Extra retry rounds for PDP-detail or validation failures before giving up (default: 5).",
    )
    parser.add_argument(
        "--allow-listing-fallback",
        action="store_true",
        default=True,
        help="Allow saving partial listing-only products after all PDP retries fail (default: True).",
    )
    parser.add_argument(
        "--no-listing-fallback",
        dest="allow_listing_fallback",
        action="store_false",
        help="Disable listing-only fallback and enforce strict full PDP only.",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip the daily inventory snapshot step at the end.",
    )
    parser.add_argument(
        "--snapshot-every-brands",
        type=int,
        default=10,
        help="Refresh today's snapshot after every N completed brands during long runs (default: 10, 0 = only final snapshot).",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore in-progress crawl checkpoints and start each brand from page 1.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    parser.add_argument(
        "--repair-colors",
        action="store_true",
        help="Repair stored product color fields from full_data_json and exit.",
    )
    parser.add_argument(
        "--repair-inventory",
        action="store_true",
        help="Repair stored size inventory counts using the latest normalization rules and exit.",
    )
    parser.add_argument(
        "--refresh-product-cache",
        action="store_true",
        help="Backfill fast product cache fields like live stock totals and primary image URLs, then exit.",
    )
    parser.add_argument(
        "--audit-data",
        action="store_true",
        help="Audit product, inventory, price, color, fabric, and size integrity, then exit.",
    )
    return parser.parse_args()



def _run_saved_sources(dry_run: bool) -> None:
    """Scrape this category's saved extra URLs (admin page → Sources) before the snapshot below,
    so Myntra/Shopify products added there land in the same snapshot as the brand scrape."""
    if dry_run:
        return
    try:
        from pathlib import Path as _Path
        sys.path.append(str(_Path(__file__).resolve().parent.parent))
        from source_scraper import run_category_sources
    except ImportError:
        return
    try:
        result = run_category_sources(take_snapshot=False)
        if result.get("sources"):
            print(json.dumps({"saved_sources": result}, indent=2, default=str))
    except Exception as exc:  # a broken source must never stop the brand scrape
        print(f"[sources] skipped: {exc}")


def main() -> int:
    args = parse_args()
    configure_scraper_logging(args.log_level)
    run_id = generate_run_id()

    if args.repair_colors:
        summary = Database().backfill_product_colors()
        print(json.dumps({"repair_colors": summary}, indent=2))
        return 0

    if args.repair_inventory:
        summary = Database().backfill_inventory_counts()
        print(json.dumps({"repair_inventory": summary}, indent=2))
        return 0

    if args.refresh_product_cache:
        summary = Database().refresh_product_cache_fields()
        print(json.dumps({"refresh_product_cache": summary}, indent=2))
        return 0

    if args.audit_data:
        summary = Database().audit_catalog_integrity()
        print(json.dumps({"audit_data": summary}, indent=2))
        return 0

    # Default: every configured brand (₹700–₹3000 segment)
    brands = (
        [b.strip() for b in args.brands.split(",") if b.strip()]
        if args.brands
        else TARGET_SHIRTS_BRANDS
    )

    print(f"\n{'='*60}")
    print(f"  ARVIND NEW GENZ SHIRTS SCRAPER — ₹{args.price_min}–₹{args.price_max}")
    print(f"  Brands ({len(brands)}): {', '.join(brands)}")
    print(f"{'='*60}\n")

    scraper = MyntraEthnicScraper(
        brands=brands,
        workers=args.workers,
        delay=args.delay,
        max_pages_per_brand=args.max_pages_per_brand,
        limit_products=args.limit_products,
        dry_run=args.dry_run,
        resume=not args.no_resume,
        pdp_retry_rounds=args.pdp_retry_rounds,
        allow_listing_fallback=args.allow_listing_fallback,
        snapshot_every_brands=args.snapshot_every_brands,
        run_id=run_id,
        price_min=args.price_min,
        price_max=args.price_max,
    )
    _run_saved_sources(args.dry_run)
    summary = scraper.run(take_snapshot=not args.no_snapshot)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
