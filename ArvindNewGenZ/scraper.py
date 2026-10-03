"""Myntra Women's Ethnic Wear scraper for a fixed brand allowlist."""

from __future__ import annotations

import json
import logging
import math
import random
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse, parse_qsl

from config import (
    BASE_DIR,
    BASE_URL,
    DEFAULT_WORKERS,
    HEADERS,
    MAX_RETRIES,
    POLITE_DELAY,
    PROXIES,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
)
from database import Database, normalize_fashion_category
from scraper_logging import ScraperTelemetry, format_duration_compact
from schema import parse_pdp_to_schema
from ethnic_taxonomy import (
    ETHNIC_PRIMARY_CATEGORIES,
    ETHNIC_CATEGORY_TREE,
    ETHNIC_LISTING_PATHS,
    TARGET_ETHNIC_BRANDS,
    BRAND_ALIASES,
    BRAND_SEED_URLS,
    classify_ethnic,
)

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - fallback for limited environments
    curl_requests = None
    import requests as std_requests
else:
    std_requests = None


LOGGER = logging.getLogger(__name__)



def _category_run_label() -> str:
    """'Myntra <category> Scraper', named from the shared categories.json (folder name as fallback)."""
    folder = Path(__file__).resolve().parent.name
    name = folder
    try:
        cats = json.loads((Path(__file__).resolve().parent.parent / "categories.json").read_text("utf-8"))["categories"]
        name = next((c["name"] for c in cats if c["folder"] == folder), folder)
    except Exception:
        pass
    return f"Myntra {name} Scraper"

def _canon(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").strip().lower())


def _slugify(text: str) -> str:
    cleaned = text.strip().lower()
    cleaned = cleaned.replace("&", " and ")
    cleaned = cleaned.replace("+", " ")
    cleaned = cleaned.replace(".", " ")
    cleaned = cleaned.replace("'", "")
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    return cleaned.strip("-")


def _alt_slugify(text: str) -> str:
    cleaned = text.strip().lower()
    cleaned = cleaned.replace("&", " ")
    cleaned = cleaned.replace("+", " ")
    cleaned = cleaned.replace("'", "")
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    return cleaned.strip("-")


ETHNIC_KEYWORDS = (
    "ethnic", "saree", "sari", "kurti", "kurta", "lehenga", "salwar", "churidar",
    "palazzo", "anarkali", "sharara", "gharara", "dupatta", "chunni", "choli",
    "blouse", "suit set", "kurta set", "co-ord", "coord", "dress", "tunic",
    "banarasi", "kanjivaram", "chanderi", "bandhani", "chikankari", "phulkari",
    "angrakha", "kaftan", "petticoat", "skirt", "patiala", "dhoti", "women"
)


def _looks_like_ethnicwear(value: str) -> bool:
    raw = (value or "").lower()
    return any(w in raw for w in ETHNIC_KEYWORDS)


_looks_like_activewear = _looks_like_ethnicwear


def _planned_total_pages(total_count: int, page_size: int) -> int:
    safe_total = max(1, int(total_count or 0))
    safe_page_size = max(1, int(page_size or 0) or 50)
    return max(1, math.ceil(safe_total / safe_page_size))


def _append_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parts = urlparse(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["p"] = str(page)
    return urlunparse(parts._replace(query=urlencode(params)))


def _json_dumps_compact(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _extract_window_json(html: str, var_name: str = "__myx") -> Dict[str, Any]:
    match = re.search(rf"window\.{re.escape(var_name)}\s*=\s*", html)
    if not match:
        raise ValueError(f"Could not find window.{var_name} assignment in HTML payload.")
    text = html[match.end():]

    depth = 0
    in_str = False
    escaped = False
    end = None
    for idx, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    if end is None:
        raise ValueError("Could not find the end of the embedded JSON blob.")
    return json.loads(text[:end])


@dataclass
class ScrapeStats:
    listing_pages: int = 0
    listing_products_seen: int = 0
    listing_products_kept: int = 0
    pdp_fetched: int = 0
    pdp_failed: int = 0
    pdp_retry_recovered: int = 0
    pdp_skipped: int = 0
    products_saved: int = 0


def _is_brand_filtered_url(url: str) -> bool:
    return "Brand%3A" in url or "Brand:" in url or "f=Brand" in url


@dataclass
class RoutePlan:
    url: str
    total_count: int
    brand_count: int
    exact_hits: int
    page_size: int
    first_page_results: Dict[str, Any]
    first_page_ids: List[int]


class MyntraEthnicScraper:
    """Scrapes Women's Ethnic Wear for a fixed allowlist of brands."""

    def __init__(
        self,
        brands: Optional[Iterable[str]] = None,
        workers: int = DEFAULT_WORKERS,
        delay: float = POLITE_DELAY,
        max_pages_per_brand: int = 0,
        limit_products: int = 0,
        dry_run: bool = False,
        resume: bool = True,
        pdp_retry_rounds: int = 4,
        allow_listing_fallback: bool = True,
        snapshot_every_brands: int = 0,
        db: Optional[Database] = None,
        run_id: Optional[str] = None,
        price_min: int = 0,
        price_max: int = 0,
    ):
        self.price_min = int(price_min or 0)
        self.price_max = int(price_max or 0)
        self.allowed_brands = list(brands or TARGET_ETHNIC_BRANDS)
        self.allowed_brand_map = {}
        for brand in self.allowed_brands:
            self.allowed_brand_map[_canon(brand)] = brand
            for alias in BRAND_ALIASES.get(brand, []):
                self.allowed_brand_map[_canon(alias)] = brand
        self.allowed_brand_set = set(self.allowed_brand_map)
        self.workers = max(1, workers)
        self.delay = max(0.0, delay)
        self.max_pages_per_brand = max_pages_per_brand
        self.limit_products = max(0, limit_products)
        self.dry_run = dry_run
        self.resume = resume
        self.pdp_retry_rounds = max(0, pdp_retry_rounds)
        self.allow_listing_fallback = allow_listing_fallback
        self.snapshot_every_brands = max(0, snapshot_every_brands)
        self.db = db or Database()
        self.run_id = run_id or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.telemetry = ScraperTelemetry(self.run_id)
        self._session_local = threading.local()
        self.stats = ScrapeStats()
        # The band passed in (main.py's --price-min/--price-max) wins; config is only a fallback.
        # Previously config overwrote it, and config defines no band, so every price was kept.
        if not (getattr(self, "price_min", 0) or getattr(self, "price_max", 0)):
            try:
                import config
                self.price_min = float(getattr(config, "PRICE_MIN", 0) or 0)
                self.price_max = float(getattr(config, "PRICE_MAX", 0) or 0)
            except Exception:
                self.price_min = 0.0
                self.price_max = 0.0
        self._seen_products: set[int] = set()
        self._resolved_routes: Dict[str, str] = {}
        self._resolved_route_plans: Dict[str, List[RoutePlan]] = {}
        self._marketplace_brand_counts: Optional[Dict[str, int]] = None
        self._processed_products_total = 0
        self._route_retry_passes = 2
        self._brand_audit_passes = 2
        self._brand_count_slack = 2
        # Bounds for the price-band pagination-wall fallback (_price_band_recover_route).
        self._PRICE_SPLIT_CEILING = 200000
        self._PRICE_SPLIT_MIN_BAND = 500
        self._PRICE_SPLIT_MAX_DEPTH = 3
        self._PRICE_SPLIT_MAX_PAGES_PER_BAND = 40
        self._counter_lock = Lock()
        self._stats_lock = Lock()
        self._failed_log_lock = Lock()
        self._failed_log_seen: set[Tuple[str, int]] = set()
        self.failed_pdp_log_path = BASE_DIR / "logs" / "failed_pdp_products.jsonl"
        self.brand_run_details: Dict[str, Dict[str, Any]] = {}
        self.brand_failures: Dict[str, str] = {}
        self._run_started_at = time.time()
        self._brand_started_at: Dict[str, float] = {}
        self._systemic_batch_min_attempts = 12
        self._systemic_batch_full_failure_attempts = 20
        self._systemic_batch_failure_ratio = 0.75

    def _schema_validation_error(self, schema: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(schema, dict):
            return "Schema payload is missing or malformed."

        product_info = schema.get("product_info") or {}
        pricing = schema.get("pricing") or {}
        inventory = schema.get("inventory_and_sizes") or {}

        product_id = int(product_info.get("product_id") or 0)
        brand = str(product_info.get("brand") or "").strip()
        title = str(product_info.get("title") or "").strip()
        category = str(product_info.get("category") or "").strip()
        product_url = str(product_info.get("product_url") or "").strip()

        if not product_id:
            return "Missing product_id in parsed schema."
        if not brand:
            return f"Product {product_id} is missing brand."
        if not title:
            return f"Product {product_id} is missing title."
        _is_valid_category = (
            category in ETHNIC_PRIMARY_CATEGORIES
            or category in {"Shirts", "Casual Shirts", "Formal Shirts", "Topwear", "Apparel"}
            or _looks_like_ethnicwear(category)
            or _looks_like_ethnicwear(title)
            or "shirt" in title.lower()
            or "shirt" in category.lower()
        )
        if not _is_valid_category:
            return f"Product {product_id} is not a valid catalog item (category={category or 'blank'}, title={title[:50]!r})."
        if not product_url.startswith("https://www.myntra.com/"):
            return f"Product {product_id} is missing a valid Myntra product URL."

        mrp = float(pricing.get("mrp") or 0.0)
        selling_price = float(pricing.get("selling_price") or 0.0)
        if mrp <= 0 or selling_price <= 0:
            return f"Product {product_id} has invalid pricing (mrp={mrp}, selling={selling_price})."
        if selling_price > mrp:
            return f"Product {product_id} has selling price above MRP (mrp={mrp}, selling={selling_price})."

        sizes = inventory.get("sizes_available") or []
        if not sizes:
            return f"Product {product_id} has no size inventory rows."

        available_sizes = 0
        available_units = 0
        for size_row in sizes:
            if not isinstance(size_row, dict):
                return f"Product {product_id} has malformed size inventory rows."
            size_label = str(size_row.get("size") or "").strip()
            if not size_label:
                return f"Product {product_id} has a blank size label."
            raw_count = size_row.get("inventory_count")
            try:
                count = int(raw_count or 0)
            except Exception:
                return f"Product {product_id} has non-numeric inventory count for size {size_label}: {raw_count!r}."
            if count < 0:
                return f"Product {product_id} has negative inventory for size {size_label}."
            available = bool(size_row.get("available"))
            if available:
                available_sizes += 1
                available_units += count
                if count <= 0:
                    return f"Product {product_id} size {size_label} is marked available with non-positive inventory."
            elif count != 0:
                return f"Product {product_id} size {size_label} is unavailable but inventory_count={count}."

        is_in_stock = bool(inventory.get("is_in_stock"))
        if is_in_stock and available_sizes == 0:
            return f"Product {product_id} is marked in stock without any available sizes."
        if is_in_stock and available_units <= 0:
            return f"Product {product_id} is marked in stock without positive inventory units."
        if not is_in_stock and available_sizes > 0:
            return f"Product {product_id} is marked out of stock but still has available sizes."

        return None

    def _log(self, level: int, message: str, *args, section: str = "general", **context: Any) -> None:
        extra = {"section": section, "run_id": self.run_id}
        for key, value in context.items():
            if value not in (None, ""):
                extra[key] = value
        LOGGER.log(level, message, *args, extra=extra)
        try:
            rendered = message % args if args else message
        except Exception:
            rendered = message
        self.telemetry.record(logging.getLevelName(level), section, rendered, **context)
        if not self.dry_run:
            try:
                self.db.log_scraper_event(
                    self.run_id,
                    logging.getLevelName(level),
                    section,
                    rendered,
                    payload=context,
                )
            except Exception:
                pass

    def _stats_totals(self) -> Dict[str, Any]:
        return {
            "listing_pages": int(self.stats.listing_pages or 0),
            "listing_products_seen": int(self.stats.listing_products_seen or 0),
            "listing_products_kept": int(self.stats.listing_products_kept or 0),
            "pdp_fetched": int(self.stats.pdp_fetched or 0),
            "pdp_failed": int(self.stats.pdp_failed or 0),
            "pdp_retry_recovered": int(self.stats.pdp_retry_recovered or 0),
            "pdp_skipped": int(self.stats.pdp_skipped or 0),
            "products_saved": int(self.stats.products_saved or 0),
            "processed_products_total": int(self._processed_products_total or 0),
        }

    def _sync_totals(self) -> None:
        self.telemetry.update_totals(self._stats_totals())

    def _update_brand_live_state(self, brand: str, **fields: Any) -> None:
        self.telemetry.update_brand(brand, **fields)
        if not self.dry_run:
            try:
                self.db.upsert_scraper_brand_progress(self.run_id, brand, fields)
            except Exception:
                pass

    def _brand_elapsed_seconds(self, brand: str) -> float:
        started_at = self._brand_started_at.get(brand)
        if not started_at:
            return 0.0
        return max(0.0, time.time() - started_at)

    def _take_precise_snapshot(self) -> Dict[str, Any]:
        if getattr(self, "_run_started_at", None):
            started_dt = datetime.fromtimestamp(self._run_started_at).astimezone()
        else:
            started_dt = datetime.now().astimezone()
        snapshot_at = started_dt.replace(microsecond=0).isoformat()
        return self.db.take_daily_snapshot(snapshot_at)
    def _parse_cookie_file(self, path: Path) -> Dict[str, str]:
        cookies_dict: Dict[str, str] = {}
        if not path.exists() or not path.is_file():
            return cookies_dict
        try:
            import http.cookiejar
            cj = http.cookiejar.MozillaCookieJar(str(path))
            cj.load(ignore_discard=True, ignore_expires=True)
            for c in cj:
                if c.name and c.value:
                    cookies_dict[c.name] = c.value
        except Exception:
            pass
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('# Netscape') or line.startswith('# https'):
                        continue
                    if line.startswith('#HttpOnly_'):
                        line = line[len('#HttpOnly_'):]
                    parts = line.split('	')
                    if len(parts) >= 7:
                        name, val = parts[5].strip(), parts[6].strip()
                        if name:
                            cookies_dict[name] = val
                    elif '=' in line and not line.startswith('#'):
                        for pair in line.split(';'):
                            if '=' in pair:
                                k, v = pair.split('=', 1)
                                k, v = k.strip(), v.strip()
                                if k:
                                    cookies_dict[k] = v
        except Exception:
            pass
        return cookies_dict

    def _build_session(self):
        headers = dict(HEADERS)
        headers["User-Agent"] = headers.get(
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        parsed_cookies: Dict[str, str] = {}
        for cookie_path in [Path("cookies.txt"), Path("../cookies.txt"), BASE_DIR / "cookies.txt", BASE_DIR.parent / "cookies.txt"]:
            if cookie_path.exists() and cookie_path.is_file():
                parsed = self._parse_cookie_file(cookie_path)
                if parsed:
                    parsed_cookies.update(parsed)
                    break
        if parsed_cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in parsed_cookies.items())
            headers["Cookie"] = cookie_str if "Cookie" not in headers else f"{headers['Cookie']}; {cookie_str}"
        if curl_requests is not None:
            session = curl_requests.Session(headers=headers)
            if PROXIES:
                session.proxies.update(PROXIES)
            return session
        session = std_requests.Session()
        session.headers.update(headers)
        if PROXIES:
            session.proxies.update(PROXIES)
        return session

    def _get_session(self):
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = self._build_session()
            self._session_local.session = session
        return session

    def _reset_session(self) -> None:
        session = getattr(self._session_local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
            self._session_local.session = None

    def _request_text(self, url: str, referer: Optional[str] = None) -> str:
        last_error: Optional[Exception] = None
        headers = {}
        if referer:
            headers["Referer"] = referer

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                session = self._get_session()
                if curl_requests is not None:
                    response = session.get(
                        url,
                        headers=headers or None,
                        timeout=REQUEST_TIMEOUT,
                        impersonate="chrome136",
                    )
                else:  # pragma: no cover - fallback path
                    response = session.get(url, headers=headers or None, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                text = response.text
                if "__myx" not in text and 'application/ld+json' not in text:
                    raise ValueError("Response did not contain recognizable Myntra page data.")
                return text
            except Exception as exc:  # pragma: no cover - network failures are environment-dependent
                last_error = exc
                self._reset_session()
                if self._wait_for_network_if_down(exc):
                    continue
                sleep_for = (RETRY_BACKOFF ** (attempt - 1)) + random.uniform(0.0, 0.35)
                self._log(
                    logging.WARNING,
                    "Request failed for %s on attempt %s/%s: %s",
                    url,
                    attempt,
                    MAX_RETRIES,
                    exc,
                    section="network",
                    attempt=f"{attempt}/{MAX_RETRIES}",
                    route=url,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(sleep_for)
        raise RuntimeError(f"Failed to fetch {url}") from last_error

    _NETWORK_DOWN_MARKERS = (
        "could not resolve host", "nodename nor servname", "name or service not known",
        "temporary failure in name resolution", "network is unreachable", "no route to host",
    )

    def _wait_for_network_if_down(self, exc: Exception, max_wait_seconds: int = 900) -> bool:
        """When the machine has lost connectivity, wait for it to return (up to 15 min) instead
        of spending retries; returns True only if it actually waited and the network is back."""
        if not any(marker in str(exc).lower() for marker in self._NETWORK_DOWN_MARKERS):
            return False
        import socket
        deadline = time.time() + max_wait_seconds
        waited = 0
        while time.time() < deadline:
            try:
                socket.getaddrinfo("www.myntra.com", 443)
                if waited:
                    self._log(logging.INFO, "Network is back after %ss; resuming.", waited, section="network")
                return waited > 0
            except OSError:
                if not waited:
                    self._log(logging.WARNING, "Network is down (%s); waiting for it to return.", exc, section="network")
                time.sleep(10)
                waited += 10
        return False

    def _product_is_stored(self, product_id: int) -> bool:
        if not product_id or self.dry_run:
            return False
        try:
            cur = self.db._get_connection().cursor()
            cur.execute("SELECT 1 FROM products WHERE product_id = ?", (product_id,))
            return cur.fetchone() is not None
        except Exception:
            return False

    def _refresh_unlisted_products(self, brand: str) -> int:
        """Re-fetch the PDP of products stored for this brand that no listing route returned in
        this run. Myntra drops sold-out items from listings and a brand's range can sit outside
        the resolved routes; without this their stock and price stay frozen at the last sighting."""
        if self.dry_run or self.limit_products or (self.max_pages_per_brand and self.max_pages_per_brand > 0):
            return 0
        canonical = self.allowed_brand_map.get(_canon(brand), brand)
        started = datetime.fromtimestamp(getattr(self, "_run_started_at", None) or time.time()).astimezone()
        try:
            cur = self.db._get_connection().cursor()
            cur.execute(
                "SELECT product_id, product_url, title FROM products WHERE brand = ? AND updated_at < ? ORDER BY product_id",
                (canonical, started),
            )
            rows = [r for r in cur.fetchall() if int(r[0]) not in self._seen_products and r[1]]
        except Exception as exc:
            self._log(logging.WARNING, "Could not list unlisted products for %s: %s", brand, exc, section="refresh", brand=brand)
            return 0
        if not rows:
            return 0
        self._log(logging.INFO, "Refreshing %s stored %s products that no listing route returned.", len(rows), brand,
                  section="refresh", brand=brand)
        saved = 0
        for start in range(0, len(rows), 40):
            items = [
                {"productId": int(pid), "landingPageUrl": str(url), "brand": canonical, "product": str(title or ""),
                 "_tracked_refresh": True}
                for pid, url, title in rows[start:start + 40]
            ]
            batch, meta = self._process_listing_items(canonical, items)
            saved += self._flush_batch(batch)
            self._seen_products.update(int(it["productId"]) for it in items)
            self._update_brand_live_state(
                brand, status="RUNNING", last_message=f"Refreshed {min(start + 40, len(rows))}/{len(rows)} unlisted products",
            )
            if meta.get("systemic_failure"):
                self._log(logging.WARNING, "Stopping unlisted refresh for %s: %s", brand, meta.get("systemic_reason"),
                          section="refresh", brand=brand)
                break
        self._log(logging.INFO, "Unlisted refresh for %s saved %s of %s products.", brand, saved, len(rows),
                  section="refresh", brand=brand)
        return saved

    def _request_json(self, url: str, headers: Optional[Dict[str, str]] = None, referer: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
        last_error: Optional[Exception] = None
        request_headers = dict(headers or {})
        if referer and "Referer" not in request_headers:
            request_headers["Referer"] = referer

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                session = self._get_session()
                if curl_requests is not None:
                    response = session.get(
                        url,
                        headers=request_headers or None,
                        timeout=REQUEST_TIMEOUT,
                        impersonate="chrome136",
                    )
                else:  # pragma: no cover - fallback path
                    response = session.get(url, headers=request_headers or None, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Listing API did not return a JSON object.")
                return payload, dict(response.headers)
            except Exception as exc:  # pragma: no cover - network failures are environment-dependent
                last_error = exc
                self._reset_session()
                if self._wait_for_network_if_down(exc):
                    continue
                sleep_for = (RETRY_BACKOFF ** (attempt - 1)) + random.uniform(0.0, 0.35)
                self._log(
                    logging.WARNING,
                    "JSON request failed for %s on attempt %s/%s: %s",
                    url,
                    attempt,
                    MAX_RETRIES,
                    exc,
                    section="network",
                    attempt=f"{attempt}/{MAX_RETRIES}",
                    route=url,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(sleep_for)
        raise RuntimeError(f"Failed to fetch JSON payload from {url}: {last_error}") from last_error

    def _build_gateway_listing_url(
        self,
        base_url: str,
        page: int,
        rows: int = 50,
        offset_override: Optional[int] = None,
        price_min_override: Optional[int] = None,
        price_max_override: Optional[int] = None,
    ) -> str:
        parts = urlparse(base_url)
        preserved_params = []
        blocked_keys = {"rows", "o", "plaEnabled", "xdEnabled", "isFacet", "p", "pincode"}
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key not in blocked_keys:
                preserved_params.append((key, value))

        offset = offset_override if offset_override is not None else rows * (page - 1)
        preserved_params.extend(
            [
                ("rows", str(rows)),
                ("o", str(offset)),
                ("plaEnabled", "false"),
                ("xdEnabled", "false"),
                ("isFacet", "true"),
                ("p", str(page)),
            ]
        )
        query = urlencode(preserved_params)
        url = f"{BASE_URL.rstrip('/')}/gateway/v4/search{parts.path}?{query}"
        # Inject price range filter into gateway URL if configured. Overrides are
        # passed explicitly (never read/write self.price_min/max here) because
        # brands are scraped concurrently via ThreadPoolExecutor and self.price_min/
        # self.price_max are shared instance state across those threads.
        effective_price_min = self.price_min if price_min_override is None else price_min_override
        effective_price_max = self.price_max if price_max_override is None else price_max_override
        if effective_price_min > 0 or effective_price_max > 0:
            price_params = []
            if effective_price_min > 0:
                price_params.append(f"priceLow={effective_price_min}")
            if effective_price_max > 0:
                price_params.append(f"priceHigh={effective_price_max}")
            url += "&" + "&".join(price_params)
        return url

    def _gateway_offset_candidates(self, page: int, rows: int = 50) -> List[int]:
        if page <= 1:
            return [0]
        candidates = [rows * (page - 1), rows * (page - 1) - 1]
        deduped: List[int] = []
        for candidate in candidates:
            if candidate >= 0 and candidate not in deduped:
                deduped.append(candidate)
        return deduped or [0]

    def _default_pagination_context(self) -> str:
        return _json_dumps_compact({"refresh": True, "v": 1})

    def _build_gateway_headers(self, referer: str, pagination_context: str) -> Dict[str, str]:
        return {
            "Accept": "application/json,text/plain,*/*",
            "Referer": referer,
            "app": "web",
            "x-myntra-app": "2160;appFamily=MyntraRetailWeb;",
            "X-Requested-With": "XMLHttpRequest",
            "pagination-context": pagination_context,
        }

    def _fetch_gateway_listing_page(self, base_url: str, page: int, pagination_context: str) -> Tuple[Dict[str, Any], Optional[str]]:
        session = self._get_session()
        # Warm Akamai cookies if not present or on first page
        if page <= 1 or not getattr(session, "cookies", None) or not session.cookies.get("bm_sz"):
            try:
                self._request_text(base_url)
            except Exception:
                pass

        headers = self._build_gateway_headers(base_url, pagination_context)
        last_error: Optional[RuntimeError] = None

        for offset in self._gateway_offset_candidates(page):
            gateway_url = self._build_gateway_listing_url(base_url, page, offset_override=offset)
            try:
                payload, response_headers = self._request_json(gateway_url, headers=headers, referer=base_url)
                next_context = response_headers.get("pagination-context")
                return payload, next_context
            except RuntimeError as exc:
                last_error = exc
                if "401" in str(exc) or "403" in str(exc):
                    self._reset_session()
                    try:
                        self._request_text(base_url)
                        payload, response_headers = self._request_json(gateway_url, headers=headers, referer=base_url)
                        next_context = response_headers.get("pagination-context")
                        return payload, next_context
                    except Exception as inner_exc:
                        last_error = inner_exc
                if "400" in str(exc):
                    continue
                break

        # Fallback to direct HTML if gateway failed
        try:
            sep = "&" if "?" in base_url else "?"
            html = self._request_text(f"{base_url}{sep}p={page}", referer=base_url)
            results = self._parse_listing_results(html)
            if results and results.get("products"):
                return results, None
        except Exception:
            pass

        raise last_error if last_error else RuntimeError(f"Failed to fetch listing page {page} for {base_url}")

    def _fetch_gateway_listing_page_with_price(
        self, base_url: str, page: int, pagination_context: str, price_min: int, price_max: int
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Price-scoped variant of _fetch_gateway_listing_page used only by the
        price-band pagination-wall fallback. Deliberately simpler than the main
        fetch path (no HTML fallback) so a failure here just ends that price band
        rather than cascading into unrelated recovery paths."""
        headers = self._build_gateway_headers(base_url, pagination_context)
        last_error: Optional[RuntimeError] = None
        for offset in self._gateway_offset_candidates(page):
            gateway_url = self._build_gateway_listing_url(
                base_url, page, offset_override=offset,
                price_min_override=price_min, price_max_override=price_max,
            )
            try:
                payload, response_headers = self._request_json(gateway_url, headers=headers, referer=base_url)
                return payload, response_headers.get("pagination-context")
            except RuntimeError as exc:
                last_error = exc
                if "400" in str(exc):
                    continue
                break
        raise last_error if last_error else RuntimeError(f"Failed to fetch price-band page {page} for {base_url}")

    def _price_band_recover_route(
        self,
        brand: str,
        base_url: str,
        route_seen_ids: set,
        brand_seen_ids: set,
        route_target_total: int,
        price_min: int = 0,
        price_max: Optional[int] = None,
        depth: int = 0,
    ) -> int:
        """Best-effort fallback for the Myntra gateway's pagination depth wall
        (HTTP 400 once the offset gets large on an unfiltered/high-volume route).
        Re-walks the same route scoped to a narrower price band so offsets stay
        low; recurses into smaller bands if a band itself still hits the wall.

        This is purely additive: it only runs after the normal route-retry passes
        are exhausted, and any failure here is caught by the caller, so the worst
        case is the same shortfall as before this fallback existed, never worse.
        Bounded by _PRICE_SPLIT_MAX_DEPTH/_PRICE_SPLIT_MIN_BAND/page cap below so
        it cannot spiral into unbounded extra requests.
        """
        if price_max is None:
            price_max = self._PRICE_SPLIT_CEILING
        if depth > self._PRICE_SPLIT_MAX_DEPTH or (price_max - price_min) < self._PRICE_SPLIT_MIN_BAND:
            return 0
        if route_target_total and len(route_seen_ids) >= route_target_total:
            return 0
        with self._counter_lock:
            if self.limit_products and self._processed_products_total >= self.limit_products:
                return 0

        recovered = 0
        page = 1
        pagination_context = self._default_pagination_context()
        hit_wall = False

        while page <= self._PRICE_SPLIT_MAX_PAGES_PER_BAND:
            with self._counter_lock:
                if self.limit_products and self._processed_products_total >= self.limit_products:
                    break
            try:
                results, next_ctx = self._fetch_gateway_listing_page_with_price(
                    base_url, page, pagination_context, price_min, price_max
                )
            except Exception:
                hit_wall = True
                break

            products = results.get("products") or []
            if not products:
                break

            fresh_items = []
            for item in products:
                product_id = int(item.get("productId") or 0)
                if not product_id or not self._listing_item_is_target(item, brand):
                    continue
                route_seen_ids.add(product_id)
                brand_seen_ids.add(product_id)
                if product_id in self._seen_products:
                    continue
                fresh_items.append(item)

            if fresh_items:
                batch, _ = self._process_listing_items(brand, fresh_items)
                flushed = self._flush_batch(batch)
                finalized_ids = {
                    int(schema.get("product_info", {}).get("product_id") or 0) for schema in batch
                }
                self._seen_products.update(pid for pid in finalized_ids if pid)
                recovered += flushed
                with self._counter_lock:
                    self._processed_products_total += len(batch)

            if route_target_total and len(route_seen_ids) >= route_target_total:
                break

            has_next = bool(results.get("hasNextPage"))
            if not has_next and not next_ctx:
                break
            pagination_context = next_ctx or pagination_context
            page += 1
            if self.delay:
                time.sleep(self.delay)

        if hit_wall and (price_max - price_min) >= self._PRICE_SPLIT_MIN_BAND * 2:
            mid = price_min + (price_max - price_min) // 2
            recovered += self._price_band_recover_route(
                brand, base_url, route_seen_ids, brand_seen_ids, route_target_total,
                price_min=price_min, price_max=mid, depth=depth + 1,
            )
            recovered += self._price_band_recover_route(
                brand, base_url, route_seen_ids, brand_seen_ids, route_target_total,
                price_min=mid, price_max=price_max, depth=depth + 1,
            )
        return recovered

    def _candidate_listing_urls(self, brand: str) -> List[str]:
        raw = brand.strip()
        urls: List[str] = []

        # 1. Pre-tested seed routes for this brand if configured
        for seed_url in BRAND_SEED_URLS.get(brand, []):
            if seed_url not in urls:
                urls.append(seed_url)

        # 2. Add brand and alias filters across primary ethnic paths (limit to top 3 aliases)
        aliases = BRAND_ALIASES.get(brand, [raw])[:3]
        for brand_name in aliases:
            slug_candidates = []
            for slug in (_slugify(brand_name), _alt_slugify(brand_name)):
                if slug and slug not in slug_candidates:
                    slug_candidates.append(slug)
                compact = slug.replace("-", "")
                if compact and compact != slug and compact not in slug_candidates:
                    slug_candidates.append(compact)

            suffixes = ("", "-shirts", "-men-shirts", "-casual-shirts")
            for slug in slug_candidates:
                for suffix in suffixes:
                    url = f"{BASE_URL.rstrip('/')}/{slug}{suffix}".rstrip("/")
                    if url and url not in urls:
                        urls.append(url)

            brand_filter_tokens = [
                f"Brand:{brand_name}",
            ]
            brand_filters = [quote(token) for token in brand_filter_tokens]
            for brand_filter in brand_filters:
                for path in ETHNIC_LISTING_PATHS[:6]:
                    url = f"{BASE_URL.rstrip('/')}/{path}?f={brand_filter}"
                    if url not in urls:
                        urls.append(url)
        return urls

    def _parse_listing_results(self, html: str) -> Dict[str, Any]:
        try:
            payload = _extract_window_json(html)
            results = (payload.get("searchData") or {}).get("results") or {}
            products = results.get("products") or []
            if isinstance(products, list):
                return results
        except Exception:
            pass

        listing_products: List[Dict[str, Any]] = []
        ld_json_blocks = re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
        for block in ld_json_blocks:
            try:
                data = json.loads(block.strip())
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("@type") != "ItemList":
                continue
            for entry in data.get("itemListElement") or []:
                if not isinstance(entry, dict):
                    continue
                product_url = str(entry.get("url") or "")
                title = str(entry.get("name") or "")
                product_id_match = re.search(r"/(\d+)(?:/buy)?/?$", product_url)
                product_id = int(product_id_match.group(1)) if product_id_match else 0
                listing_products.append(
                    {
                        "productId": product_id,
                        "productName": title,
                        "product": title,
                        "landingPageUrl": product_url,
                    }
                )
        if listing_products:
            return {
                "products": listing_products,
                "totalCount": len(listing_products),
                "totalProductCount": len(listing_products),
                "hasNextPage": False,
                "filters": {},
            }
        raise ValueError("Listing payload did not contain a usable products array.")

    def _parse_pdp_payload(self, html: str) -> Dict[str, Any]:
        try:
            payload = _extract_window_json(html)
            pdp_data = payload.get("pdpData") or {}
            if pdp_data:
                return pdp_data
        except Exception:
            pass

        ld_json_blocks = re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
        for block in ld_json_blocks:
            try:
                data = json.loads(block.strip())
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("@type") != "Product":
                continue
            brand = data.get("brand") or {}
            offers = data.get("offers") or {}
            offer_url = offers.get("url") if isinstance(offers, dict) else ""
            return {
                "id": data.get("sku") or data.get("mpn"),
                "name": data.get("name"),
                "brand": {"name": brand.get("name") if isinstance(brand, dict) else str(brand or "")},
                "landingPageUrl": offer_url,
                "description": data.get("description"),
                "price": {
                    "discounted": offers.get("price") if isinstance(offers, dict) else 0,
                    "mrp": offers.get("price") if isinstance(offers, dict) else 0,
                },
                "mrp": offers.get("price") if isinstance(offers, dict) else 0,
                "images": [{"src": data.get("image")}],
                "offers": [offers] if offers else [],
                "analytics": {"category": "Ethnic Wear", "subCategory": "Kurtis & Kurtas"},
                "articleAttributes": {},
                "sizes": [],
                "ratings": {},
            }
        raise ValueError("PDP payload did not contain pdpData.")

    def _extract_brand_filter_count(self, results: Dict[str, Any], brand: str) -> int:
        filters = results.get("filters") or {}
        primary_filters = filters.get("primaryFilters") or []
        target = _canon(brand)
        for filt in primary_filters:
            if not isinstance(filt, dict):
                continue
            filter_name = str(filt.get("id") or filt.get("title") or "").strip().lower()
            if filter_name != "brand":
                continue
            values = filt.get("filterValues") or filt.get("values") or []
            for value in values:
                if not isinstance(value, dict):
                    continue
                val_str = str(value.get("value") or value.get("id") or "")
                if self._brand_matches_target(val_str, brand) or _canon(val_str) == target:
                    return int(value.get("count") or 0)
        return 0

    def _extract_all_brand_filter_counts(self, results: Dict[str, Any]) -> Dict[str, int]:
        filters = results.get("filters") or {}
        primary_filters = filters.get("primaryFilters") or []
        brand_counts: Dict[str, int] = {}
        for filt in primary_filters:
            if not isinstance(filt, dict):
                continue
            filter_name = str(filt.get("id") or filt.get("title") or "").strip().lower()
            if filter_name != "brand":
                continue
            values = filt.get("filterValues") or filt.get("values") or []
            for value in values:
                if not isinstance(value, dict):
                    continue
                brand_name = str(value.get("value") or value.get("id") or "").strip()
                if not brand_name:
                    continue
                brand_counts[brand_name] = int(value.get("count") or 0)
        return brand_counts

    def _discover_marketplace_brand_counts(self) -> Dict[str, int]:
        if self._marketplace_brand_counts is not None:
            return self._marketplace_brand_counts

        for url in (
            *[f"{BASE_URL.rstrip('/')}/{path}" for path in ETHNIC_LISTING_PATHS],
            
        ):
            try:
                html = self._request_text(url)
                results = self._parse_listing_results(html)
                brand_counts = self._extract_all_brand_filter_counts(results)
                if brand_counts:
                    self._marketplace_brand_counts = brand_counts
                    return brand_counts
            except Exception as exc:
                self._log(
                    logging.WARNING,
                    "Marketplace brand discovery failed for %s: %s",
                    url,
                    exc,
                    section="run",
                    route=url,
                )

        self._marketplace_brand_counts = {}
        return self._marketplace_brand_counts

    def _find_marketplace_missing_brands(self) -> List[str]:
        brand_counts = self._discover_marketplace_brand_counts()
        available = {_canon(brand) for brand in brand_counts}
        return [brand for brand in self.allowed_brands if _canon(brand) not in available]

    def _brand_matches_target(self, candidate_brand: str, target_brand: str) -> bool:
        if _canon(candidate_brand) == _canon(target_brand):
            return True
        aliases = BRAND_ALIASES.get(target_brand, [])
        return any(_canon(candidate_brand) == _canon(alias) for alias in aliases)

    def _listing_item_is_target(self, item: Dict[str, Any], target_brand: str) -> bool:
        candidate_brand = str(item.get("brand") or "")
        if candidate_brand:
            if not self._brand_matches_target(candidate_brand, target_brand):
                return False
        else:
            title_text = " ".join(
                [
                    str(item.get("product") or ""),
                    str(item.get("productName") or ""),
                    str(item.get("landingPageUrl") or ""),
                ]
            )
            aliases = BRAND_ALIASES.get(target_brand, [target_brand])
            if not any(_canon(a) in _canon(title_text) for a in aliases):
                return False

        # Price range post-filter (applied when price_min/price_max configured)
        if self.price_min > 0 or self.price_max > 0:
            raw_price = item.get("discountedPrice") or item.get("price") or 0
            try:
                item_price = float(raw_price)
                if self.price_min > 0 and item_price < self.price_min:
                    return False
                if self.price_max > 0 and item_price > self.price_max:
                    return False
            except (TypeError, ValueError):
                pass

        # Shirts only. Myntra's own articleType is exact ("Shirts" vs "Tshirts" / "Sweatshirts" /
        # "Kurtas"), so trust it when the listing carries one. The text fallback below matched
        # "shirt" inside "T-shirt" and "Sweatshirt", which let T-shirts in as shirts.
        article_type = re.sub(r"[^a-z]", "", str(item.get("articleType") or "").lower())
        if article_type:
            return article_type == "shirts"
        not_a_shirt = re.compile(r"\bt[\s-]?shirts?\b|\btees?\b|\bsweat[\s-]?shirts?\b")  # \b: "Velvet Shirt" is a shirt
        if any(not_a_shirt.search(str(item.get(k) or "").lower()) for k in ("product", "productName", "landingPageUrl")):
            return False
        SHIRT_SIGNALS = {
            "shirt", "casual shirt", "formal shirt", "casual shirts", "formal shirts",
            "shirts", "printed shirt", "solid shirt", "linen shirt", "checks",
        }
        category_signals = [
            str(item.get("category") or "").lower(),
            str(item.get("articleType") or "").lower(),
            str(item.get("product") or "").lower(),
            str(item.get("productName") or "").lower(),
            str(item.get("landingPageUrl") or "").lower(),
        ]
        return any(
            any(sig in field for sig in SHIRT_SIGNALS)
            for field in category_signals
        )

    def _resolve_brand_routes(self, brand: str) -> List[RoutePlan]:
        if brand in self._resolved_route_plans:
            return self._resolved_route_plans[brand]

        seed_urls = BRAND_SEED_URLS.get(brand, [])
        all_candidate_urls = self._candidate_listing_urls(brand)

        candidate_batches = []
        if seed_urls:
            candidate_batches.append(seed_urls)
            fallback = [u for u in all_candidate_urls if u not in seed_urls]
            if fallback:
                candidate_batches.append(fallback)
        else:
            candidate_batches.append(all_candidate_urls)

        candidates: List[RoutePlan] = []
        for batch in candidate_batches:
            for candidate in batch:
                try:
                    # The gateway API applies ?f= filters; the server-rendered HTML ignores them,
                    # so its totals and facets describe the unfiltered listing.
                    try:
                        results, _ = self._fetch_gateway_listing_page(candidate, 1, "")
                    except Exception:
                        results = self._parse_listing_results(self._request_text(candidate))
                    products = results.get("products") or []
                except Exception as exc:
                    LOGGER.debug("Route candidate failed for %s -> %s: %s", brand, candidate, exc)
                    continue

                first_page_ids: List[int] = []
                exact_hits = 0
                for item in products:
                    product_id = int(item.get("productId") or 0)
                    if self._listing_item_is_target(item, brand):
                        exact_hits += 1
                        if product_id:
                            first_page_ids.append(product_id)

                brand_count = self._extract_brand_filter_count(results, brand)
                total = int(results.get("totalCount") or results.get("totalProductCount") or 0)
                if total and _is_brand_filtered_url(candidate):
                    # The gateway applies the brand filter, so the listing's own total is the exact
                    # brand count for this route; the facet can describe a different scope.
                    brand_count = total
                if exact_hits <= 0 and brand_count <= 0:
                    continue
                candidates.append(
                    RoutePlan(
                        url=candidate,
                        total_count=total,
                        brand_count=brand_count,
                        exact_hits=exact_hits,
                        page_size=max(1, len(products)),
                        first_page_results=results,
                        first_page_ids=first_page_ids,
                    )
                )
            if candidates:
                break

        if not candidates:
            raise RuntimeError(f"Could not resolve a Myntra listing URL for brand '{brand}'.")

        exact_brand_candidates = [
            route for route in candidates
            if route.brand_count > 0 and route.total_count > 0 and (
                abs(route.total_count - route.brand_count) <= 25
                or (route.brand_count / max(route.total_count, 1)) >= 0.90
            )
        ]
        if exact_brand_candidates:
            candidates = exact_brand_candidates

        candidates.sort(
            key=lambda route: (
                1 if any(k in route.url.lower() for k in ("ethnic", "kurt", "saree", "suit")) else 0,
                route.exact_hits,
                route.brand_count,
                "Brand%3A" in route.url or "Brand:" in route.url,
            ),
            reverse=True,
        )
        if exact_brand_candidates:
            selected = list(candidates)
            self._resolved_routes[brand] = selected[0].url
            self._resolved_route_plans[brand] = selected
            self._log(
                logging.INFO,
                "Resolved %s -> %s routes (best brand total=%s): %s",
                brand,
                len(selected),
                max((route.brand_count for route in selected), default=0),
                ", ".join(route.url for route in selected),
                section="brand",
                brand=brand,
                expected=max((route.brand_count for route in selected), default=0),
            )
            return selected

        selected: List[RoutePlan] = []
        seen_seed_ids: set[int] = set()
        max_brand_count = 0

        for route in candidates:
            route_ids = {pid for pid in route.first_page_ids if pid}
            adds_new_ids = bool(route_ids - seen_seed_ids)
            materially_bigger = route.brand_count > max_brand_count
            if not selected or adds_new_ids or materially_bigger:
                selected.append(route)
                seen_seed_ids.update(route_ids)
                max_brand_count = max(max_brand_count, route.brand_count)

        self._resolved_routes[brand] = selected[0].url
        self._resolved_route_plans[brand] = selected
        self._log(
            logging.INFO,
            "Resolved %s -> %s routes (best brand total=%s): %s",
            brand,
            len(selected),
            max_brand_count,
            ", ".join(route.url for route in selected),
            section="brand",
            brand=brand,
            expected=max_brand_count,
        )
        return selected

    def _build_fallback_schema(self, item: Dict[str, Any]) -> Dict[str, Any]:
        inventory_info = item.get("inventoryInfo") or []
        listing_brand = str(item.get("brand") or "")
        rating_count = int(item.get("ratingCount") or 0)
        rating_value = float(item.get("rating") or 0.0)

        price = item.get("price")
        discounted_price = item.get("discountedPrice")
        if discounted_price in (None, "") and price not in (None, ""):
            discount_amount = item.get("discount") or 0
            try:
                discounted_price = max(0, float(price) - float(discount_amount))
            except Exception:
                discounted_price = price

        pdp_like = {
            "id": item.get("productId"),
            "name": item.get("productName") or item.get("product"),
            "brand": listing_brand,
            "landingPageUrl": item.get("landingPageUrl"),
            "baseColour": item.get("baseColour") or item.get("baseColor") or item.get("colour") or item.get("color"),
            "colours": item.get("colours") or item.get("colors") or [],
            "analytics": {
                "category": item.get("category") or "Kurtis & Kurtas",
                "subCategory": item.get("subCategory") or "Kurtas",
                "gender": item.get("gender") or "Women",
            },
            "articleType": {"typeName": item.get("articleType") or "Kurtas"},
            "price": {
                "mrp": price or discounted_price or 0,
                "discounted": discounted_price or price or 0,
            },
            "images": [{"src": item.get("searchImage")}],
            "ratings": {
                "averageRating": rating_value,
                "totalCount": rating_count,
                "reviewInfo": {"reviewsCount": item.get("reviewCount") or 0},
            },
            "sizes": [
                {
                    "label": inv.get("label"),
                    "skuId": inv.get("skuId"),
                    "available": inv.get("available"),
                    "inventory": inv.get("inventory") or 0,
                    "sizeSellerData": [
                        {
                            "availableCount": inv.get("inventory") or 0,
                            "sellableInventoryCount": inv.get("inventory") or 0,
                        }
                    ],
                }
                for inv in inventory_info
            ],
        }

        schema = parse_pdp_to_schema(pdp_like, raw_url=self._listing_url(item))
        schema["product_info"]["brand"] = listing_brand
        schema["product_info"]["category"] = schema["product_info"].get("category") or "Kurtis & Kurtas"
        schema["raw_source"] = {
            "source_type": "listing_fallback",
            "is_full_payload": False,
            "payload": item,
        }
        return schema

    def _listing_url(self, item: Dict[str, Any]) -> str:
        landing_page = str(item.get("landingPageUrl") or "").lstrip("/")
        return urljoin(BASE_URL.rstrip("/") + "/", landing_page)

    def _listing_to_schema(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        product_url = self._listing_url(item)
        try:
            html = self._request_text(product_url, referer=BASE_URL)
            pdp_data = self._parse_pdp_payload(html)
            schema = parse_pdp_to_schema(pdp_data, raw_url=product_url)
        except Exception as exc:
            raise RuntimeError(f"PDP fetch failed for {product_url}: {exc}") from exc
        finalized = self._finalize_schema(schema, item, product_url)
        if finalized is None:
            return None
        validation_error = self._schema_validation_error(finalized)
        if validation_error:
            raise RuntimeError(f"PDP validation failed for {product_url}: {validation_error}")
        return finalized

    def _finalize_schema(
        self,
        schema: Dict[str, Any],
        item: Dict[str, Any],
        product_url: str,
    ) -> Optional[Dict[str, Any]]:
        brand_name = str(schema["product_info"].get("brand") or item.get("brand") or "")
        category_name = normalize_fashion_category(
            str(schema["product_info"].get("category") or ""),
            str(schema["product_info"].get("title") or ""),
            str(schema["product_info"].get("product_url") or product_url),
            str(schema["product_info"].get("sub_category") or ""),
        )

        if self.allowed_brand_set and _canon(brand_name) not in self.allowed_brand_set:
            return None

        # Price range filter (strict ₹700–₹3000 enforcement) for new products. A product already
        # tracked keeps being refreshed when its price drifts out of band, so its record stays true.
        price_val = 0.0 if item.get("_tracked_refresh") else float(schema.get("pricing", {}).get("selling_price") or schema.get("product_info", {}).get("selling_price") or item.get("discountedPrice") or item.get("price") or 0)
        if getattr(self, 'price_min', 0) > 0 and price_val < self.price_min:
            return None
        if getattr(self, 'price_max', 0) > 0 and price_val > self.price_max:
            return None

        _product_title = str(schema["product_info"].get("title") or "").lower()
        _is_valid = (
            category_name in ETHNIC_PRIMARY_CATEGORIES
            or category_name in {"Shirts", "Casual Shirts", "Formal Shirts", "Topwear", "Apparel"}
            or _looks_like_ethnicwear(category_name)
            or _looks_like_ethnicwear(_product_title)
            or "shirt" in _product_title
            or "shirt" in category_name.lower()
        )
        if not _is_valid:
            return None

        canonical_brand = self.allowed_brand_map.get(_canon(brand_name), brand_name)
        schema["product_info"]["brand"] = canonical_brand
        schema["product_info"]["category"] = schema["product_info"].get("category") or category_name or "Shirts"
        schema["product_info"]["product_url"] = product_url
        return schema

    def _fetch_listing_item_result(
        self,
        item: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[str], bool]:
        try:
            schema = self._listing_to_schema(item)
            if not schema:
                return item, None, "Parsed PDP did not match the approved ethnic wear brand/category filters.", False
            return item, schema, None, bool(schema)
        except Exception as exc:
            return item, None, str(exc), False

    def _assess_batch_health(
        self,
        attempted: int,
        saved_candidates: int,
        unresolved: int,
        skipped: int,
    ) -> Dict[str, Any]:
        failure_ratio = round((unresolved / attempted), 4) if attempted > 0 else 0.0
        systemic_failure = False
        systemic_reason = ""

        if attempted >= self._systemic_batch_full_failure_attempts and unresolved == attempted:
            systemic_failure = True
            systemic_reason = (
                f"All {attempted} products in the batch failed PDP/detail validation after "
                f"{self.pdp_retry_rounds + 1} attempts."
            )
        elif attempted >= self._systemic_batch_full_failure_attempts and failure_ratio >= self._systemic_batch_failure_ratio:
            systemic_failure = True
            systemic_reason = (
                f"{unresolved}/{attempted} products ({round(failure_ratio * 100, 1)}%) failed PDP/detail validation "
                f"after {self.pdp_retry_rounds + 1} attempts."
            )
        elif attempted >= self._systemic_batch_min_attempts and saved_candidates == 0 and unresolved >= self._systemic_batch_min_attempts:
            systemic_failure = True
            systemic_reason = (
                f"No valid products survived in a batch of {attempted}; unresolved failures remained at {unresolved}."
            )

        return {
            "attempted": attempted,
            "saved_candidates": saved_candidates,
            "unresolved": unresolved,
            "skipped": skipped,
            "failure_ratio": failure_ratio,
            "systemic_failure": systemic_failure,
            "systemic_reason": systemic_reason,
        }

    def _append_failed_pdp_log(self, brand: str, item: Dict[str, Any], error: str) -> None:
        product_id = int(item.get("productId") or 0)
        payload = {
            "brand": brand,
            "product_id": product_id or item.get("productId"),
            "product_url": self._listing_url(item),
            "product_name": item.get("productName") or item.get("product"),
            "logged_at": int(time.time()),
            "error": error,
        }
        try:
            self.failed_pdp_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._failed_log_lock:
                dedupe_key = (brand, product_id)
                if dedupe_key in self._failed_log_seen:
                    return
                self._failed_log_seen.add(dedupe_key)
                with self.failed_pdp_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:
            self._log(
                logging.WARNING,
                "Could not write failed PDP retry log for %s: %s",
                payload["product_url"],
                exc,
                section="pdp",
                brand=brand,
                route=payload["product_url"],
            )

    def _process_listing_items(self, brand: str, items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        pending = list(items)
        collected: List[Dict[str, Any]] = []
        recovered_after_retry = 0
        fetched_successfully = 0
        batch_started_at = time.time()
        skipped_due_to_failure = 0
        unresolved_after_handling = 0

        for attempt_idx in range(self.pdp_retry_rounds + 1):
            if not pending:
                break

            if attempt_idx > 0:
                self._log(
                    logging.INFO,
                    "Retrying %s PDP fetches for %s (retry round %s/%s).",
                    len(pending),
                    brand,
                    attempt_idx,
                    self.pdp_retry_rounds,
                    section="pdp",
                    brand=brand,
                    attempt=f"{attempt_idx}/{self.pdp_retry_rounds}",
                )
                time.sleep(min(2.0, self.delay + attempt_idx * 0.5))

            next_pending: List[Dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                future_map = {executor.submit(self._fetch_listing_item_result, item): item for item in pending}
                for future in as_completed(future_map):
                    item, schema, error, fetched = future.result()
                    if schema:
                        if attempt_idx > 0:
                            recovered_after_retry += 1
                        collected.append(schema)
                        if fetched:
                            fetched_successfully += 1
                    else:
                        # Scope/validation rejections are deterministic: retrying only repeats the request.
                        if not (error and ("did not match the approved" in error or "PDP validation failed" in error)):
                            next_pending.append(item)
                        product_id = int(item.get("productId") or 0)
                        self._log(
                            logging.WARNING,
                            "%s",
                            error,
                            section="pdp",
                            brand=brand,
                            route=self._listing_url(item),
                        )
                        if not self.dry_run and error:
                            try:
                                self.db.log_scraper_error(self.run_id, product_id or None, "PDP_FETCH", error)
                            except Exception:
                                pass
            pending = next_pending

        with self._stats_lock:
            if fetched_successfully:
                self.stats.pdp_fetched += fetched_successfully
            if recovered_after_retry:
                self.stats.pdp_retry_recovered += recovered_after_retry

        if pending:
            with self._stats_lock:
                self.stats.pdp_failed += len(pending)
            self._sync_totals()
            for item in pending:
                product_url = self._listing_url(item)
                error = f"Full PDP unavailable after {self.pdp_retry_rounds + 1} attempts"
                if self.allow_listing_fallback and not self._product_is_stored(int(item.get("productId") or 0)):
                    self._log(
                        logging.WARNING,
                        "Using listing fallback for %s after retries.",
                        product_url,
                        section="pdp",
                        brand=brand,
                        route=product_url,
                    )
                    fallback_schema = self._build_fallback_schema(item)
                    finalized = self._finalize_schema(fallback_schema, item, product_url)
                    validation_error = self._schema_validation_error(finalized)
                    if finalized and not validation_error:
                        collected.append(finalized)
                    else:
                        unresolved_after_handling += 1
                        if validation_error:
                            error = f"{error}; fallback validation failed: {validation_error}"
                            self._log(
                                logging.WARNING,
                                "Skipping listing fallback for %s because validation failed: %s",
                                product_url,
                                validation_error,
                                section="pdp",
                                brand=brand,
                                route=product_url,
                            )
                            with self._stats_lock:
                                self.stats.pdp_skipped += 1
                            skipped_due_to_failure += 1
                else:
                    unresolved_after_handling += 1
                    self._log(
                        logging.WARNING,
                        "Skipping %s because full PDP data was still unavailable after retries.",
                        product_url,
                        section="pdp",
                        brand=brand,
                        route=product_url,
                    )
                    with self._stats_lock:
                        self.stats.pdp_skipped += 1
                    skipped_due_to_failure += 1
                self._append_failed_pdp_log(brand, item, error)

        self._sync_totals()
        batch_meta = self._assess_batch_health(
            attempted=len(items),
            saved_candidates=len(collected),
            unresolved=unresolved_after_handling,
            skipped=skipped_due_to_failure,
        )

        batch_elapsed_seconds = max(0.0, time.time() - batch_started_at)
        if items:
            products_per_minute = round((len(collected) / batch_elapsed_seconds) * 60.0, 2) if batch_elapsed_seconds > 0 else 0.0
            avg_seconds_per_product = round((batch_elapsed_seconds / len(collected)), 3) if collected else 0.0
            self._log(
                logging.INFO,
                "Processed %s target products for %s in %s (%s products/min).",
                len(collected),
                brand,
                format_duration_compact(batch_elapsed_seconds),
                products_per_minute,
                section="pdp",
                brand=brand,
                saved=len(collected),
                elapsed_seconds=round(batch_elapsed_seconds, 2),
                elapsed_label=format_duration_compact(batch_elapsed_seconds),
                products_per_minute=products_per_minute,
                avg_seconds_per_product=avg_seconds_per_product,
                failed=batch_meta["unresolved"],
                skipped=batch_meta["skipped"],
                failure_ratio=batch_meta["failure_ratio"],
            )

        return collected, batch_meta

    def _flush_batch(self, batch: List[Dict[str, Any]]) -> int:
        if not batch:
            return 0
        validated_batch: List[Dict[str, Any]] = []
        rejected_count = 0
        for schema in batch:
            validation_error = self._schema_validation_error(schema)
            if validation_error:
                rejected_count += 1
                product_info = schema.get("product_info") or {}
                product_id = int(product_info.get("product_id") or 0) or None
                brand = str(product_info.get("brand") or "").strip() or None
                route = str(product_info.get("product_url") or "").strip() or None
                self._log(
                    logging.WARNING,
                    "Dropping product before save: %s",
                    validation_error,
                    section="db",
                    brand=brand,
                    route=route,
                    product_id=product_id,
                )
                if not self.dry_run and product_id:
                    try:
                        self.db.log_scraper_error(self.run_id, product_id, "SCHEMA_VALIDATION", validation_error)
                    except Exception:
                        pass
                with self._stats_lock:
                    self.stats.pdp_skipped += 1
                continue
            validated_batch.append(schema)
        if rejected_count:
            self._sync_totals()
        if not validated_batch:
            return 0
        if self.dry_run:
            self._log(logging.INFO, "Dry run: prepared %s products for save.", len(validated_batch), section="db", saved=len(validated_batch))
            return len(validated_batch)
        saved = self.db.save_products_batch(validated_batch)
        self.stats.products_saved += saved
        self._sync_totals()
        return saved

    def _resume_page_for_brand(self, brand: str, route_url: str) -> int:
        if not self.resume:
            return 1
        state = self.db.get_crawl_state("women_ethnic", f"{brand}::{route_url}")
        if not state:
            return 1
        if str(state.get("status") or "").upper() != "IN_PROGRESS":
            return 1
        page = int(state.get("page") or 1)
        return max(1, page)

    def _clear_brand_resolution_cache(self, brand: str) -> None:
        self._resolved_routes.pop(brand, None)
        self._resolved_route_plans.pop(brand, None)

    def _get_brand_db_count(self, brand: str) -> int:
        canonical = self.allowed_brand_map.get(_canon(brand), brand)
        return self.db.get_brand_product_count(canonical)

    def _is_brand_complete(self, brand: str) -> Tuple[bool, Dict[str, Any]]:
        canonical = self.allowed_brand_map.get(_canon(brand), brand)
        details = self.brand_run_details.get(canonical, {})
        expected = int(details.get("expected_brand_total") or 0)
        db_count = self._get_brand_db_count(canonical)
        shortfall = max(0, expected - db_count)
        is_capped_run = bool(
            (self.limit_products and self._processed_products_total >= self.limit_products)
            or (self.max_pages_per_brand and self.max_pages_per_brand > 0)
        )
        slack = max(self._brand_count_slack, int(expected * 0.03))
        is_complete = is_capped_run or expected <= 0 or shortfall <= slack
        # Routes overlap, so a summed "expected" overstates the brand. The exact test is per
        # route: every route reached its own live total without a fetch error.
        routes = details.get("routes_scraped") or []
        if not is_complete and routes:
            def _route_done(r: Dict[str, Any]) -> bool:
                total, facet = int(r.get("route_total_count") or 0), int(r.get("route_brand_count") or 0)
                target = min(total, facet) if (total and facet) else (total or facet)
                seen = int(r.get("route_unique_target_products_seen") or 0)
                if self.price_min > 0 or self.price_max > 0:
                    # Myntra ignores priceLow/priceHigh, so route totals count every price while
                    # only in-band items are kept: a route walked to its end without errors is complete.
                    return not r.get("route_fetch_error")
                return not r.get("route_fetch_error") and seen >= target - max(self._brand_count_slack, int(target * 0.03))
            if all(_route_done(r) for r in routes):
                is_complete = True
                shortfall = 0
        audit = {
            "brand": canonical,
            "expected_brand_total": expected,
            "db_product_count": db_count,
            "shortfall": 0 if is_capped_run else shortfall,
            "is_complete": is_complete,
        }
        details["db_product_count"] = db_count
        details["audit_shortfall"] = 0 if is_capped_run else shortfall
        details["audit_complete"] = is_complete
        self.brand_run_details[canonical] = details
        return is_complete, audit

    def scrape_brand(self, brand: str) -> int:
        self._brand_started_at[brand] = time.time()
        saved_total = 0
        processed_total = 0
        brand_seen_ids: set[int] = set()
        expected_brand_total = 0
        route_metrics: List[Dict[str, Any]] = []
        self.telemetry.set_current_brand(brand)
        self._update_brand_live_state(
            brand,
            status="RUNNING",
            expected_brand_total=expected_brand_total,
            unique_target_products_seen=0,
            saved_products=0,
            route_count=0,
            completion_ratio=0.0,
            last_message="Resolving brand routes",
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        self._log(
            logging.INFO,
            "Resolving brand routes.",
            section="brand",
            brand=brand,
        )

        route_plans = self._resolve_brand_routes(brand)
        unique_pids: set[int] = set()
        expected_brand_total = 0
        for r in route_plans:
            if r.first_page_ids:
                already = sum(1 for pid in r.first_page_ids if pid in unique_pids)
                if (already / len(r.first_page_ids)) < 0.5:
                    unique_pids.update(r.first_page_ids)
                    expected_brand_total += r.brand_count
        if not expected_brand_total:
            expected_brand_total = max((route.brand_count for route in route_plans), default=0)

        self._update_brand_live_state(
            brand,
            expected_brand_total=expected_brand_total,
            route_count=len(route_plans),
            last_message="Brand scrape started",
        )
        self._log(
            logging.INFO,
            "Starting brand scrape.",
            section="brand",
            brand=brand,
            expected=expected_brand_total,
        )

        for route in route_plans:
            with self._counter_lock:
                if self.limit_products and self._processed_products_total >= self.limit_products:
                    break

            # Skip redundant route if its first page items are already substantially captured
            if route.first_page_ids and len(route.first_page_ids) >= 10:
                seen_ratio = sum(1 for pid in route.first_page_ids if pid in brand_seen_ids) / len(route.first_page_ids)
                if seen_ratio >= 0.85:
                    self._log(
                        logging.INFO,
                        "Skipping redundant route %s for %s (%.0f%% of first page already captured).",
                        route.url,
                        brand,
                        seen_ratio * 100,
                        section="brand",
                        brand=brand,
                        route=route.url,
                    )
                    continue

            base_url = route.url
            route_seen_ids: set[int] = set()
            route_total_pages = _planned_total_pages(max(route.total_count or route.brand_count, 1), route.page_size)
            if route.brand_count and route.total_count:
                route_target_total = min(route.brand_count, route.total_count)
            else:
                route_target_total = max(route.brand_count, route.total_count, 0)
            route_fetch_error: Optional[str] = None
            empty_page_retries: Dict[int, int] = {}
            route_slack = max(self._brand_count_slack, int(route_target_total * 0.02))

            for route_pass in range(self._route_retry_passes + 1):
                with self._counter_lock:
                    if self.limit_products and self._processed_products_total >= self.limit_products:
                        break
                if route.brand_count and len(route_seen_ids) >= (route.brand_count - route_slack):
                    break
                if route_pass > 0:
                    self._log(
                        logging.WARNING,
                        "Retrying route %s for %s (pass %s/%s) after incomplete coverage: %s/%s unique products seen.",
                        base_url,
                        brand,
                        route_pass,
                        self._route_retry_passes,
                        len(route_seen_ids),
                        route_target_total or "?",
                        section="listing",
                        brand=brand,
                        route=base_url,
                        attempt=f"{route_pass}/{self._route_retry_passes}",
                        seen=len(route_seen_ids),
                        expected=route_target_total or 0,
                    )
                    self._reset_session()

                resume_page = self._resume_page_for_brand(brand, base_url) if route_pass == 0 else 1
                page = 1
                pagination_context = self._default_pagination_context()
                stalled_pages = 0
                route_failed_this_pass = False

                while True:
                    if self.max_pages_per_brand and page > self.max_pages_per_brand:
                        break
                    with self._counter_lock:
                        if self.limit_products and self._processed_products_total >= self.limit_products:
                            break

                    page_url = self._build_gateway_listing_url(base_url, page)
                    self._log(
                        logging.INFO,
                        "Listing %s route %s page %s -> %s",
                        brand,
                        base_url,
                        page,
                        page_url,
                        section="listing",
                        brand=brand,
                        route=base_url,
                        page=page,
                        seen=len(route_seen_ids),
                        expected=route_target_total or 0,
                    )
                    try:
                        results, next_pagination_context = self._fetch_gateway_listing_page(base_url, page, pagination_context)
                    except RuntimeError as exc:
                        if page > 1 and route.total_count and (page - 1) * max(route.page_size, 1) >= route.total_count:
                            break  # asked past the end of the listing (Myntra answers HTTP 400)
                        route_fetch_error = str(exc)
                        route_failed_this_pass = True
                        self._log(
                            logging.WARNING,
                            "Route fetch failed for %s page %s (%s). Will retry with another pass or fallback route.",
                            base_url,
                            page,
                            exc,
                            section="listing",
                            brand=brand,
                            route=base_url,
                            page=page,
                        )
                        self._reset_session()
                        break
                    products = results.get("products") or []
                    if not products:
                        if page < route_total_pages:
                            # Under load Myntra can answer with an empty list instead of an error (a soft
                            # block). Retry the same page on a fresh session before giving it up.
                            soft_retries = empty_page_retries.get(page, 0)
                            if soft_retries < 3:
                                empty_page_retries[page] = soft_retries + 1
                                wait_seconds = (5, 15, 30)[soft_retries]
                                self._log(
                                    logging.WARNING,
                                    "Empty listing page for %s on page %s before expected end; retrying in %ss (attempt %s/3).",
                                    brand, page, wait_seconds, soft_retries + 1,
                                    section="listing", brand=brand, route=base_url, page=page,
                                )
                                self._reset_session()
                                time.sleep(wait_seconds)
                                continue
                            self._log(
                                logging.WARNING,
                                "Empty listing page for %s on page %s before expected end; continuing.",
                                brand,
                                page,
                                section="listing",
                                brand=brand,
                                route=base_url,
                                page=page,
                            )
                            if next_pagination_context:
                                pagination_context = next_pagination_context
                            page += 1
                            continue
                        break

                    self.stats.listing_pages += 1
                    self.stats.listing_products_seen += len(products)
                    self._sync_totals()

                    fresh_target_items = []
                    page_candidate_ids: set[int] = set()
                    new_route_ids_on_page = 0
                    for item in products:
                        product_id = int(item.get("productId") or 0)
                        if not product_id:
                            continue
                        if not self._listing_item_is_target(item, brand):
                            continue
                        if product_id not in route_seen_ids:
                            new_route_ids_on_page += 1
                        route_seen_ids.add(product_id)
                        brand_seen_ids.add(product_id)
                        if page < resume_page:
                            continue
                        if product_id in page_candidate_ids:
                            continue
                        if product_id in self._seen_products:
                            continue
                        page_candidate_ids.add(product_id)
                        fresh_target_items.append(item)

                    if page < resume_page:
                        self._log(
                            logging.INFO,
                            "Replayed %s route %s page %s to rebuild pagination context before resume page %s.",
                            brand,
                            base_url,
                            page,
                            resume_page,
                            section="listing",
                            brand=brand,
                            route=base_url,
                            page=page,
                        )
                    else:
                        self.stats.listing_products_kept += len(fresh_target_items)
                        if fresh_target_items:
                            remaining = 0
                            with self._counter_lock:
                                if self.limit_products:
                                    remaining = max(0, self.limit_products - self._processed_products_total)
                            if self.limit_products and remaining <= 0:
                                break
                            if self.limit_products and remaining < len(fresh_target_items):
                                fresh_target_items = fresh_target_items[:remaining]

                            batch, batch_meta = self._process_listing_items(brand, fresh_target_items)
                            flushed = self._flush_batch(batch)
                            finalized_ids = {
                                int(schema.get("product_info", {}).get("product_id") or 0)
                                for schema in batch
                            }
                            self._seen_products.update(pid for pid in finalized_ids if pid)
                            saved_total += flushed
                            processed_total += len(batch)
                            with self._counter_lock:
                                self._processed_products_total += len(batch)
                            self._sync_totals()
                            if batch_meta.get("systemic_failure"):
                                route_fetch_error = batch_meta.get("systemic_reason") or "Systemic PDP failure detected."
                                route_failed_this_pass = True
                                self._log(
                                    logging.WARNING,
                                    "Stopping current route pass for %s because batch health failed guardrails: %s",
                                    brand,
                                    route_fetch_error,
                                    section="pdp",
                                    brand=brand,
                                    route=base_url,
                                    page=page,
                                    seen=batch_meta.get("attempted"),
                                    saved=batch_meta.get("saved_candidates"),
                                    failed=batch_meta.get("unresolved"),
                                    failure_ratio=batch_meta.get("failure_ratio"),
                                )
                                self._reset_session()
                                break

                    self._update_brand_live_state(
                        brand,
                        status="RUNNING",
                        page=page,
                        route=base_url,
                        seen_products=len(brand_seen_ids),
                        saved_products=saved_total,
                        unique_target_products_seen=len(brand_seen_ids),
                        completion_ratio=round((len(brand_seen_ids) / expected_brand_total), 4) if expected_brand_total else 0.0,
                        last_message=f"Route page {page} processed",
                        elapsed_seconds=round(self._brand_elapsed_seconds(brand), 2),
                        elapsed_label=format_duration_compact(self._brand_elapsed_seconds(brand)),
                    )

                    current_brand_count = self._extract_brand_filter_count(results, brand)
                    if current_brand_count:
                        expected_brand_total = max(expected_brand_total, current_brand_count)
                        listing_total = int(results.get("totalCount") or results.get("totalProductCount") or 0)
                        route_target_total = max(route_target_total, min(current_brand_count, listing_total) if listing_total else current_brand_count)
                    current_total_count = int(results.get("totalCount") or results.get("totalProductCount") or route.total_count or 0)
                    if current_total_count:
                        current_page_size = max(len(products), route.page_size, 1)
                        route_total_pages = max(route_total_pages, _planned_total_pages(current_total_count, current_page_size))

                    if not self.dry_run:
                        self.db.save_crawl_state("women_ethnic", f"{brand}::{base_url}", page, processed_total, status="IN_PROGRESS")

                    declared_has_next_page = results.get("hasNextPage")
                    if declared_has_next_page is None:
                        has_next_page = page < route_total_pages
                    else:
                        has_next_page = bool(declared_has_next_page)
                        if current_total_count and page < route_total_pages:
                            has_next_page = True
                    if new_route_ids_on_page == 0 and route_target_total and len(route_seen_ids) < route_target_total:
                        stalled_pages += 1
                    else:
                        stalled_pages = 0

                    if stalled_pages >= 3 and has_next_page:
                        self._log(
                            logging.WARNING,
                            "Route %s for %s stalled for %s consecutive pages before reaching expected total; restarting route pass.",
                            base_url,
                            brand,
                            stalled_pages,
                            section="listing",
                            brand=brand,
                            route=base_url,
                            page=page,
                            seen=len(route_seen_ids),
                            expected=route_target_total or 0,
                        )
                        break

                    if page >= route_total_pages and not has_next_page:
                        break
                    if page >= route_total_pages and not next_pagination_context:
                        break
                    if not has_next_page and not next_pagination_context and new_route_ids_on_page == 0:
                        break
                    pagination_context = next_pagination_context or pagination_context
                    page += 1
                    if self.delay:
                        time.sleep(self.delay)

                if route_failed_this_pass:
                    continue
                if not route_target_total or len(route_seen_ids) >= route_target_total:
                    break

            if (
                not self.dry_run
                and route_target_total
                and len(route_seen_ids) < route_target_total * 0.95
            ):
                try:
                    extra_recovered = self._price_band_recover_route(
                        brand, base_url, route_seen_ids, brand_seen_ids, route_target_total
                    )
                except Exception as exc:
                    extra_recovered = 0
                    self._log(
                        logging.WARNING,
                        "Price-band fallback errored for %s on %s (continuing with normal results): %s",
                        brand, base_url, exc,
                        section="listing", brand=brand, route=base_url,
                    )
                if extra_recovered:
                    saved_total += extra_recovered
                    processed_total += extra_recovered
                    self._log(
                        logging.INFO,
                        "Price-band fallback recovered %s additional products for %s on %s (now %s/%s).",
                        extra_recovered, brand, base_url, len(route_seen_ids), route_target_total or "?",
                        section="listing", brand=brand, route=base_url,
                        seen=len(route_seen_ids), expected=route_target_total or 0,
                    )

            route_metrics.append(
                {
                    "url": base_url,
                    "route_total_count": route.total_count,
                    "route_brand_count": route.brand_count,
                    "route_unique_target_products_seen": len(route_seen_ids),
                    "route_total_pages_planned": route_total_pages,
                    "route_completion_ratio": round((len(route_seen_ids) / route_target_total), 4) if route_target_total else 0.0,
                    "route_fetch_error": route_fetch_error,
                }
            )
            if not self.dry_run:
                self.db.save_crawl_state("women_ethnic", f"{brand}::{base_url}", page, processed_total, status="COMPLETED")
            with self._counter_lock:
                if self.limit_products and self._processed_products_total >= self.limit_products:
                    break

        hit_product_limit = bool(self.limit_products and self._processed_products_total >= self.limit_products)
        hit_page_limit = bool(self.max_pages_per_brand and self.max_pages_per_brand > 0)
        is_capped_run = hit_product_limit or hit_page_limit

        if expected_brand_total and len(brand_seen_ids) < expected_brand_total and not is_capped_run:
            self._log(
                logging.WARNING,
                "Brand %s finished below expected count: saw %s unique ethnic wear products vs expected %s.",
                brand,
                len(brand_seen_ids),
                expected_brand_total,
                section="brand",
                brand=brand,
                seen=len(brand_seen_ids),
                expected=expected_brand_total,
            )

        saved_total += self._refresh_unlisted_products(brand)

        prev_details = self.brand_run_details.get(brand, {})
        final_seen = max(int(prev_details.get("unique_target_products_seen") or 0), len(brand_seen_ids))
        final_saved = int(prev_details.get("saved_products") or 0) + saved_total if saved_total > 0 else (int(prev_details.get("saved_products") or 0) or saved_total)
        final_routes = route_metrics if route_metrics else prev_details.get("routes_scraped", [])

        self.brand_run_details[brand] = {
            "expected_brand_total": expected_brand_total,
            "unique_target_products_seen": final_seen,
            "brand_completion_ratio": round((final_seen / expected_brand_total), 4) if expected_brand_total else 0.0,
            "routes_scraped": final_routes,
            "saved_products": final_saved,
            "duration_seconds": round(self._brand_elapsed_seconds(brand), 2),
            "duration_label": format_duration_compact(self._brand_elapsed_seconds(brand)),
            "products_per_minute": round((final_saved / self._brand_elapsed_seconds(brand)) * 60.0, 2) if self._brand_elapsed_seconds(brand) > 0 else 0.0,
        }
        brand_duration_seconds = self._brand_elapsed_seconds(brand)
        brand_products_per_minute = round((final_saved / brand_duration_seconds) * 60.0, 2) if brand_duration_seconds > 0 else 0.0
        self._update_brand_live_state(
            brand,
            status="COMPLETED",
            seen_products=final_seen,
            saved_products=final_saved,
            expected_brand_total=expected_brand_total,
            unique_target_products_seen=final_seen,
            completion_ratio=round((final_seen / expected_brand_total), 4) if expected_brand_total else 0.0,
            routes_scraped=final_routes,
            last_message="Brand scrape completed",
            finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            duration_seconds=round(brand_duration_seconds, 2),
            duration_label=format_duration_compact(brand_duration_seconds),
            products_per_minute=brand_products_per_minute,
        )
        self._log(
            logging.INFO,
            "Brand scrape completed in %s (%s products/min).",
            format_duration_compact(brand_duration_seconds),
            brand_products_per_minute,
            section="brand",
            brand=brand,
            saved=final_saved,
            seen=final_seen,
            expected=expected_brand_total,
            status="COMPLETED",
            elapsed_seconds=round(brand_duration_seconds, 2),
            elapsed_label=format_duration_compact(brand_duration_seconds),
            products_per_minute=brand_products_per_minute,
        )
        return saved_total

    def run(self, take_snapshot: bool = True) -> Dict[str, Any]:
        self.db.log_scraper_run(
            run_type=_category_run_label(),
            status="RUNNING",
            total_items=0,
            successful_items=0,
            failed_items=0,
            rate=0.0,
            duration=0.0,
            log_summary=f"Starting scraper for {len(self.allowed_brands)} approved brands.",
            run_id=self.run_id,
        )
        self.telemetry.start_run({
            "brands_requested": len(self.allowed_brands),
            "workers": self.workers,
            "delay_seconds": self.delay,
            "dry_run": self.dry_run,
            "resume": self.resume,
            "pdp_retry_rounds": self.pdp_retry_rounds,
            "allow_listing_fallback": self.allow_listing_fallback,
            "snapshot_every_brands": self.snapshot_every_brands,
            "take_snapshot": bool(take_snapshot),
        })
        summary = {
            "run_id": self.run_id,
            "brands_requested": len(self.allowed_brands),
            "brands_completed": 0,
            "saved_products": 0,
            "listing_pages": 0,
        }
        periodic_snapshot_errors: List[str] = []
        periodic_snapshots: List[Dict[str, Any]] = []

        should_skip_preflight = (
            len(self.allowed_brands) <= 3
            or self.max_pages_per_brand > 0
            or self.limit_products > 0
        )
        if should_skip_preflight:
            marketplace_brand_counts = {}
            marketplace_missing = []
            self._log(logging.INFO, "Skipping marketplace brand preflight for focused/test run.", section="run")
        else:
            try:
                marketplace_brand_counts = self._discover_marketplace_brand_counts()
                canonical_marketplace_brands = {_canon(name) for name in marketplace_brand_counts}
                marketplace_missing = [
                    brand for brand in self.allowed_brands
                    if _canon(brand) not in canonical_marketplace_brands
                ]
            except Exception as exc:
                marketplace_brand_counts = {}
                marketplace_missing = []
                self._log(logging.WARNING, "Could not preflight marketplace brand availability: %s", exc, section="run")
        summary["brands_not_currently_listed_on_myntra"] = marketplace_missing
        summary["marketplace_available_brand_count"] = len(marketplace_brand_counts)
        self._sync_totals()

        for brand in self.allowed_brands:
            with self._counter_lock:
                if self.limit_products and self._processed_products_total >= self.limit_products:
                    self._log(logging.INFO, "Reached global product limit (%s). Stopping brand iteration.", self.limit_products, section="run")
                    break
            try:
                saved = self.scrape_brand(brand)
                summary["brands_completed"] += 1
                summary["saved_products"] += saved
                self.telemetry.update_totals({
                    **self._stats_totals(),
                    "brands_requested": len(self.allowed_brands),
                    "brands_completed": summary["brands_completed"],
                    "saved_products_this_run": summary["saved_products"],
                })
                if (
                    take_snapshot
                    and not self.dry_run
                    and self.snapshot_every_brands > 0
                    and summary["brands_completed"] % self.snapshot_every_brands == 0
                ):
                    try:
                        snapshot = self._take_precise_snapshot()
                        periodic_snapshots.append(snapshot)
                        self._log(
                            logging.INFO,
                            "Periodic snapshot created after %s.",
                            brand,
                            section="snapshot",
                            brand=brand,
                        )
                    except Exception as exc:
                        message = f"{brand}: {exc}"
                        periodic_snapshot_errors.append(message)
                        self._log(logging.WARNING, "Periodic snapshot creation failed after %s: %s", brand, exc, section="snapshot", brand=brand)
            except Exception as exc:
                self.brand_failures[brand] = str(exc)
                brand_duration_seconds = self._brand_elapsed_seconds(brand)
                self._update_brand_live_state(
                    brand,
                    status="FAILED",
                    last_message=str(exc),
                    finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    duration_seconds=round(brand_duration_seconds, 2),
                    duration_label=format_duration_compact(brand_duration_seconds),
                )
                self._log(logging.ERROR, "Brand scrape failed for %s: %s", brand, exc, section="brand", brand=brand, status="FAILED")
                if not self.dry_run:
                    try:
                        self.db.log_scraper_error(self.run_id, None, "BRAND_RUN", f"{brand}: {exc}")
                    except Exception:
                        pass
                continue

        brand_audit_runs: List[Dict[str, Any]] = []
        final_brand_audit: List[Dict[str, Any]] = []
        is_capped_run = bool(
            (self.limit_products and self._processed_products_total >= self.limit_products)
            or (self.max_pages_per_brand and self.max_pages_per_brand > 0)
        )
        if not self.dry_run and not is_capped_run:
            for audit_pass in range(1, self._brand_audit_passes + 1):
                incomplete_brands: List[Tuple[str, Dict[str, Any]]] = []
                for brand in self.allowed_brands:
                    try:
                        is_complete, audit = self._is_brand_complete(brand)
                        if not is_complete:
                            incomplete_brands.append((brand, audit))
                    except Exception as exc:
                        self._log(logging.WARNING, "Brand completeness audit failed for %s: %s", brand, exc, section="audit", brand=brand)
                brand_audit_runs.append(
                    {
                        "pass": audit_pass,
                        "incomplete_brands": [audit for _, audit in incomplete_brands],
                    }
                )
                if not incomplete_brands:
                    break

                self._log(
                    logging.WARNING,
                    "Audit pass %s found %s incomplete brands; retrying them: %s",
                    audit_pass,
                    len(incomplete_brands),
                    ", ".join(audit["brand"] for _, audit in incomplete_brands),
                    section="audit",
                    attempt=audit_pass,
                )
                for brand, audit in incomplete_brands:
                    try:
                        self._clear_brand_resolution_cache(brand)
                        self._reset_session()
                        saved = self.scrape_brand(brand)
                        summary["saved_products"] += saved
                        self._log(
                            logging.INFO,
                            "Audit retry completed for %s: saved=%s, shortfall_before=%s",
                            audit["brand"],
                            saved,
                            audit["shortfall"],
                            section="audit",
                            brand=audit["brand"],
                            saved=saved,
                        )
                    except Exception as exc:
                        self._log(logging.ERROR, "Audit retry failed for %s: %s", brand, exc, section="audit", brand=brand)

            for brand in self.allowed_brands:
                try:
                    _, audit = self._is_brand_complete(brand)
                    final_brand_audit.append(audit)
                except Exception as exc:
                    final_brand_audit.append(
                        {
                            "brand": brand,
                            "expected_brand_total": 0,
                            "db_product_count": 0,
                            "shortfall": 0,
                            "is_complete": False,
                            "audit_error": str(exc),
                        }
                    )

        summary["listing_pages"] = self.stats.listing_pages
        summary["listing_products_seen"] = self.stats.listing_products_seen
        summary["listing_products_kept"] = self.stats.listing_products_kept
        summary["pdp_fetched"] = self.stats.pdp_fetched
        summary["pdp_failed"] = self.stats.pdp_failed
        summary["pdp_retry_recovered"] = self.stats.pdp_retry_recovered
        summary["pdp_skipped"] = self.stats.pdp_skipped
        summary["products_saved"] = self.stats.products_saved
        summary["strict_full_pdp_only"] = not self.allow_listing_fallback
        summary["failed_pdp_log"] = str(self.failed_pdp_log_path)
        summary["brand_run_details"] = self.brand_run_details
        summary["brand_failures"] = self.brand_failures
        summary["brands_failed"] = sorted(self.brand_failures)
        summary["brand_audit_runs"] = brand_audit_runs
        summary["brand_audit_final"] = final_brand_audit
        summary["brands_incomplete_after_audit"] = [
            audit["brand"] for audit in final_brand_audit if not audit.get("is_complete")
        ]
        summary["missing_target_brands_in_db"] = [
            brand for brand in self.allowed_brands if self._get_brand_db_count(brand) <= 0
        ]
        summary["periodic_snapshots"] = periodic_snapshots
        summary["periodic_snapshot_errors"] = periodic_snapshot_errors

        if not self.dry_run:
            try:
                summary["inventory_repair"] = self.db.backfill_inventory_counts()
                self._log(logging.INFO, "Final inventory repair completed.", section="inventory")
            except Exception as exc:
                self._log(logging.WARNING, "Final inventory repair failed: %s", exc, section="inventory")
                summary["inventory_repair_error"] = str(exc)

        if take_snapshot and not self.dry_run:
            try:
                summary["snapshot"] = self._take_precise_snapshot()
                self._log(logging.INFO, "Final snapshot created.", section="snapshot")
            except Exception as exc:
                self._log(logging.WARNING, "Daily snapshot creation failed: %s", exc, section="snapshot")
                summary["snapshot_error"] = str(exc)

        duration = round(max(0.0, time.time() - self._run_started_at), 2)
        summary["duration_seconds"] = duration
        has_audit_shortfalls = bool(
            not is_capped_run
            and summary.get("brands_incomplete_after_audit")
        )
        status = "COMPLETED_WITH_ERRORS" if (self.brand_failures or has_audit_shortfalls) else "COMPLETED"
        total_items = int(summary.get("listing_products_seen") or 0)
        successful_items = int(summary.get("products_saved") or 0)
        failed_items = int(self.stats.pdp_failed or 0) + len(self.brand_failures)
        rate = round((successful_items / duration), 2) if duration > 0 else 0.0
        summary["run_status"] = status
        self.db.log_scraper_run(
            run_type=_category_run_label(),
            status=status,
            total_items=total_items,
            successful_items=successful_items,
            failed_items=failed_items,
            rate=rate,
            duration=duration,
            log_summary=(
                f"brands_completed={summary['brands_completed']}/{summary['brands_requested']} | "
                f"products_saved={successful_items:,} | listing_seen={total_items:,} | "
                f"pdp_failed={int(self.stats.pdp_failed or 0):,} | brand_failures={len(self.brand_failures)}"
            ),
            run_id=self.run_id,
        )
        self.telemetry.update_totals({
            **self._stats_totals(),
            "brands_requested": len(self.allowed_brands),
            "brands_completed": summary["brands_completed"],
            "saved_products_this_run": summary["saved_products"],
            "duration_seconds": duration,
        })
        self.telemetry.finish_run(status, summary=summary)

        return summary

    def scrape_product_ids(self, product_ids: List[int], take_snapshot: bool = True) -> Dict[str, Any]:
        """Directly fetches and persists specific product IDs into the database."""
        self._run_started_at = time.time()
        self.db.log_scraper_run(
            run_type="Myntra Product ID Scraper",
            status="RUNNING",
            total_items=len(product_ids),
            successful_items=0,
            failed_items=0,
            rate=0.0,
            duration=0.0,
            log_summary=f"Starting direct scrape for {len(product_ids)} product IDs.",
            run_id=self.run_id,
        )
        saved_products = []
        failed_products = []
        all_schemas = []

        for idx, pid in enumerate(product_ids, 1):
            product_url = f"{BASE_URL.rstrip('/')}/{pid}"
            self._log(logging.INFO, f"[{idx}/{len(product_ids)}] Fetching product {pid}...", section="pdp", route=product_url)
            try:
                html = self._request_text(product_url, referer=BASE_URL)
                pdp_data = self._parse_pdp_payload(html)
                schema = parse_pdp_to_schema(pdp_data, raw_url=product_url)
                validation_error = self._schema_validation_error(schema)
                if validation_error:
                    raise RuntimeError(f"Validation error: {validation_error}")

                if not self.dry_run:
                    ok = self.db.save_product(schema)
                    if not ok:
                        raise RuntimeError(f"Database save_product returned False for {pid}")

                p_info = schema.get("product_info", {})
                pricing = schema.get("pricing", {})
                saved_products.append({
                    "product_id": pid,
                    "brand": p_info.get("brand"),
                    "title": p_info.get("title"),
                    "category": p_info.get("category"),
                    "sub_category": p_info.get("sub_category"),
                    "selling_price": pricing.get("selling_price"),
                    "mrp": pricing.get("mrp"),
                    "discount_percentage": pricing.get("discount_percentage"),
                    "in_stock": schema.get("inventory_and_sizes", {}).get("is_in_stock", True),
                    "size_count": len(schema.get("inventory_and_sizes", {}).get("sizes_available", [])),
                })
                all_schemas.append(schema)
                self._log(logging.INFO, f"[✓] Scraped product {pid}: {p_info.get('brand')} - {p_info.get('title')}", section="pdp", product_id=pid)
            except Exception as exc:
                self._log(logging.ERROR, f"[✗] Failed product {pid}: {exc}", section="pdp", product_id=pid)
                failed_products.append({"product_id": pid, "error": str(exc)})

            if self.delay > 0:
                time.sleep(self.delay)

        # Refresh brands table & snapshots
        if not self.dry_run and saved_products:
            self.db.populate_brands_table()
            if take_snapshot:
                try:
                    self._take_precise_snapshot()
                except Exception as exc:
                    self._log(logging.WARNING, f"Snapshot creation notice: {exc}", section="snapshot")

            # Export to JSONL and CSV
            try:
                from config import JSONL_PATH, CSV_PATH
                import csv
                JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(JSONL_PATH, "w", encoding="utf-8") as f_jsonl:
                    for s in all_schemas:
                        f_jsonl.write(json.dumps(s, ensure_ascii=False) + "\n")
                
                with open(CSV_PATH, "w", newline="", encoding="utf-8") as f_csv:
                    writer = csv.DictWriter(f_csv, fieldnames=[
                        "product_id", "brand", "title", "category", "sub_category",
                        "selling_price", "mrp", "discount_percentage", "in_stock", "size_count"
                    ])
                    writer.writeheader()
                    for sp in saved_products:
                        writer.writerow(sp)
            except Exception as exc:
                self._log(logging.WARNING, f"Export generation notice: {exc}", section="general")

        duration = round(max(0.0, time.time() - self._run_started_at), 2)
        status = "COMPLETED" if not failed_products else ("COMPLETED_WITH_ERRORS" if saved_products else "FAILED")

        summary = {
            "run_id": self.run_id,
            "run_status": status,
            "total_requested": len(product_ids),
            "successful_count": len(saved_products),
            "failed_count": len(failed_products),
            "duration_seconds": duration,
            "saved_products": saved_products,
            "failed_products": failed_products,
        }

        self.db.log_scraper_run(
            run_type="Myntra Product ID Scraper",
            status=status,
            total_items=len(product_ids),
            successful_items=len(saved_products),
            failed_items=len(failed_products),
            rate=round(len(saved_products) / duration, 2) if duration > 0 else 0.0,
            duration=duration,
            log_summary=f"Scraped {len(saved_products)}/{len(product_ids)} products successfully.",
            run_id=self.run_id,
        )

        return summary



# Backwards compatibility for older imports.
MyntraActivewearScraper = MyntraEthnicScraper
MyntraPoloScraper = MyntraEthnicScraper
MyntraFormalShoesScraper = MyntraEthnicScraper
MyntraShirtBrandScraper = MyntraEthnicScraper

