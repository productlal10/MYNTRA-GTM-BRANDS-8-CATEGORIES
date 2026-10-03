"""Brand catalog management for the Women's Ethnic Wear intelligence project."""

import json
import csv
from typing import Dict, Any, List, Set, Tuple

from config import DATA_DIR
from database import Database
from ethnic_taxonomy import ETHNIC_LISTING_PATHS, TARGET_ETHNIC_BRANDS

# Known Myntra In-House Private Labels, FWD Labels, and Exclusive Brands
MYNTRA_INHOUSE_BRANDS = {
    "anouk",
    "sangria",
    "taavi",
    "house of pataudi",
    "all about you",
    "all about you by deepika padukone",
    "roadster",
    "the roadster life co.",
    "the roadster lifestyle co",
    "the roadster lifestyle co.",
    "r.code by the roadster life co.",
    "mast & harbour",
    "mast and harbour",
    "here&now",
    "here & now",
    "moda rapido",
    "dressberry",
    "kook n keech",
    "kook & keech",
    "ether",
    "hrx",
    "hrx by hrithik roshan",
    "invictus",
    "sztori",
    "harvard",
    "mod & shy",
    "friskers",
    "u&f",
    "glitchez",
    "stylecast",
    "stylecast x revolte",
    "fwd",
}

BRANDS_CSV_PATH = DATA_DIR / "brands_directory.csv"
BRANDS_JSON_PATH = DATA_DIR / "brands_directory.json"


def is_myntra_label_brand(brand_name: str, system_attrs: List[Any] = None) -> bool:
    if not brand_name:
        return False

    b_clean = brand_name.lower().strip()
    for inhouse in MYNTRA_INHOUSE_BRANDS:
        if inhouse in b_clean or b_clean == inhouse:
            return True

    if system_attrs:
        for attr in system_attrs:
            if isinstance(attr, dict):
                a_name = str(attr.get("attribute", "")).upper()
                a_val = str(attr.get("value", "")).lower()
                if "MYNTRA_UNIQUE" in a_name or "myntra" in a_val:
                    return True

    return False


def get_brand_classification(brand_name: str, system_attrs: List[Any] = None) -> Tuple[bool, str]:
    is_myntra = is_myntra_label_brand(brand_name, system_attrs)
    return is_myntra, ("Myntra In-House Label" if is_myntra else "Non-Myntra Brand")


class BrandManager:
    """Discovers, catalogues, and indexes Myntra brands for Women's Ethnic."""

    def __init__(self, db: Database = None):
        self.db = db or Database()

    def discover_all_brands(self, scraper) -> List[Dict[str, Any]]:
        print("\n" + "=" * 60)
        print("DISCOVERING ALL BRANDS LISTED ON MYNTRA (WOMEN'S ETHNIC)")
        print("=" * 60)

        all_brands_map: Dict[str, Dict[str, Any]] = {}
        categories = ETHNIC_LISTING_PATHS

        for category in categories:
            print(f"[*] Querying all brands in {category}...")
            discovered = scraper.discover_category_brands(category)
            for brand_row in discovered:
                brand_name = brand_row.get("id") or brand_row.get("value")
                if not brand_name:
                    continue
                count = int(brand_row.get("count", 0) or 0)
                is_myntra, brand_type = get_brand_classification(brand_name)
                existing = all_brands_map.setdefault(
                    brand_name,
                    {
                        "brand_name": brand_name,
                        "ethnic_count": 0,
                        "total_count": 0,
                        "is_myntra_label": is_myntra,
                        "brand_type": brand_type,
                    },
                )
                existing["ethnic_count"] += count
                existing["total_count"] += count

        sorted_brands = sorted(all_brands_map.values(), key=lambda x: x["total_count"], reverse=True)
        print(f"[✓] Successfully discovered {len(sorted_brands):,} distinct Women's Ethnic brands!")

        self.save_brands_to_db(sorted_brands)
        self.export_brands_directory(sorted_brands)
        return sorted_brands

    def save_brands_to_db(self, brands_list: List[Dict[str, Any]]):
        conn = self.db._get_connection()
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS brands (
                    brand_name TEXT PRIMARY KEY,
                    ethnic_count INTEGER DEFAULT 0,
                    total_count INTEGER DEFAULT 0,
                    is_myntra_label INTEGER DEFAULT 0,
                    brand_type TEXT,
                    crawled_status TEXT DEFAULT 'PENDING',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            records = [
                (
                    b["brand_name"],
                    b.get("ethnic_count", b.get("total_count", 0)),
                    b["total_count"],
                    1 if b["is_myntra_label"] else 0,
                    b["brand_type"],
                )
                for b in brands_list
            ]
            conn.executemany(
                """
                INSERT INTO brands (brand_name, ethnic_count, total_count, is_myntra_label, brand_type)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(brand_name) DO UPDATE SET
                    ethnic_count=excluded.ethnic_count,
                    total_count=excluded.total_count,
                    is_myntra_label=excluded.is_myntra_label,
                    brand_type=excluded.brand_type,
                    updated_at=CURRENT_TIMESTAMP;
                """,
                records,
            )

    def export_brands_directory(self, brands_list: List[Dict[str, Any]]):
        with open(BRANDS_CSV_PATH, "w", encoding="utf-8", newline="") as f_csv:
            writer = csv.DictWriter(
                f_csv,
                fieldnames=["brand_name", "brand_type", "is_myntra_label", "ethnic_count", "total_count"],
                extrasaction="ignore",
            )
            writer.writeheader()
            for brand in brands_list:
                writer.writerow(brand)

        with open(BRANDS_JSON_PATH, "w", encoding="utf-8") as f_json:
            json.dump(brands_list, f_json, indent=2, ensure_ascii=False)

    def get_brands_by_filter(self, brand_type_filter: str = "all") -> List[str]:
        conn = self.db._get_connection()
        cur = conn.cursor()
        if brand_type_filter == "myntra":
            cur.execute("SELECT brand_name FROM brands WHERE is_myntra_label = 1 ORDER BY brand_name ASC")
        elif brand_type_filter == "non-myntra":
            cur.execute("SELECT brand_name FROM brands WHERE is_myntra_label = 0 ORDER BY brand_name ASC")
        else:
            cur.execute("SELECT brand_name FROM brands ORDER BY brand_name ASC")
        return [row[0] for row in cur.fetchall() if row and row[0]]
