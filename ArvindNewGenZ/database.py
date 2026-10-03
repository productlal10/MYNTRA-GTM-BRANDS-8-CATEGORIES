"""Database storage and persistence layer backed by PostgreSQL."""

import ast
import json
import random
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, Any, Optional, Set, List
import threading
import os
from contextlib import contextmanager

from pg_schema import DDL as PG_SCHEMA_DDL
from schema import extract_primary_color, normalize_color_name, derive_color_hex, normalize_inventory_count, normalize_inventory_entries, normalize_fabric

# PostgreSQL compatibility shim
# - Converts ? placeholders → %s
# - Preserves row-style access by index and column name
# - Silently accepts row_factory assignments from older call sites
try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.extensions
    _PG_AVAILABLE = True

    # Automatically cast PostgreSQL DECIMAL/NUMERIC to Python float.
    _DEC2FLOAT = psycopg2.extensions.new_type(
        psycopg2.extensions.DECIMAL.values,
        'DEC2FLOAT',
        lambda value, curs: float(value) if value is not None else None
    )
    psycopg2.extensions.register_type(_DEC2FLOAT)
except ImportError:
    _PG_AVAILABLE = False


import re
from urllib.parse import urlparse



def _safe_parse_json(raw, default=None):
    if raw in (None, ""):
        return default if default is not None else {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        try:
            return ast.literal_eval(raw)
        except Exception:
            return default if default is not None else {}


def _extract_primary_image_from_product_data(product_data: Dict[str, Any]) -> str:
    media = (product_data or {}).get("media") or {}
    primary = media.get("primary_image")
    return str(primary or "").strip()


def _summarize_inventory_rows(size_rows: List[Dict[str, Any]]) -> tuple[int, int]:
    total_stock = 0
    available_size_count = 0
    for row in size_rows or []:
        available = bool((row or {}).get("available"))
        try:
            count = int((row or {}).get("inventory_count") or 0)
        except Exception:
            count = 0
        if available and count > 0:
            available_size_count += 1
            total_stock += count
    return total_stock, available_size_count


def _coerce_iso_timestamp(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).astimezone()
    token = str(value).strip()
    if not token:
        return None
    try:
        return datetime.fromisoformat(token)
    except ValueError:
        try:
            return datetime.fromisoformat(token.replace("Z", "+00:00"))
        except ValueError:
            return None



# Products selling at least this many units per day are fast movers (same rule as the
# per-snapshot stock_status in take_daily_snapshot).
FAST_MOVER_ROS = 4.0


# A stock drop is booked as sales only when it plausibly is one. Each threshold can be
# overridden per category in its .env (e.g. SALES_MAX_UNITS_PER_DAY=300).
#   size_pull_min      a size that goes unavailable while holding at least this many units
#                      was pulled, not sold (needs size-level snapshots)
#   collapse_*         without size history: stock falling by collapse_share or more from at
#                      least collapse_min_stock units in one snapshot is treated as pulled
#   max_units_per_day  a single style "selling" faster than this is not credible
_SALES_RULE_DEFAULTS = {
    "size_pull_min": ("SALES_SIZE_PULL_MIN", 50),
    "collapse_min_stock": ("SALES_COLLAPSE_MIN_STOCK", 100),
    "collapse_share": ("SALES_COLLAPSE_SHARE", 0.9),
    "max_units_per_day": ("SALES_MAX_UNITS_PER_DAY", 500.0),
    # Seller-declared stock pools (e.g. 5000 per size): the level is not physical stock, but
    # small decrements (5000 -> 4998) are orders. Larger drops are re-declarations.
    "pool_max_units_per_day": ("SALES_POOL_MAX_UNITS_PER_DAY", 100.0),
    "pool_max_share": ("SALES_POOL_MAX_SHARE", 0.1),
    # Products whose stock is a normalized marketplace placeholder: a drop above this is an artifact.
    "placeholder_max_drop": ("SALES_PLACEHOLDER_MAX_DROP", 200),
    # Data Issues report only (does not change sales): a rise this big in one scrape is flagged
    # as a suspicious restock (relisting / placeholder) rather than trusted.
    "suspect_restock_min": ("SALES_SUSPECT_RESTOCK_MIN", 500),
    # ...or this many units AND at least this multiple of the stock it had (10 -> 400).
    "suspect_restock_ratio_min": ("SALES_SUSPECT_RESTOCK_RATIO_MIN", 100),
    "suspect_restock_ratio": ("SALES_SUSPECT_RESTOCK_RATIO", 5.0),
    # Data Issues report only: a counted drop this big whose stock comes back (this share of it)
    # at the next scrape was most likely pulled and re-listed, not sold.
    "bounce_min_units": ("SALES_BOUNCE_MIN_UNITS", 100),
    "bounce_share": ("SALES_BOUNCE_SHARE", 0.8),
}


def sales_rules() -> Dict[str, Any]:
    rules = {}
    for key, (env, default) in _SALES_RULE_DEFAULTS.items():
        raw = os.environ.get(env, "").strip()
        try:
            value = type(default)(raw) if raw else default
        except ValueError:
            value = default
        rules[key] = value if value > 0 else default
    return rules


def analytics_window(cur, days: int):
    """Stored analytics for the last `days` days, anchored on the latest snapshot.

    Returns (window_dates, span_days, prior_dates, prior_span_days). span_days is the
    time actually observed: from the snapshot just before the window (the baseline the
    first window snapshot was compared against) to the latest one in the window. Rate of
    sale is units sold / span_days, a true per-day rate however often snapshots run;
    averaging the per-snapshot ros column is not, because snapshot gaps vary.
    """
    cur.execute("SELECT DISTINCT analytics_date FROM daily_sales_analytics ORDER BY analytics_date ASC;")
    dates = [r[0] for r in cur.fetchall() if r[0]]
    if not dates:
        return [], 0.0, [], 0.0
    days = max(1, int(days or 1))
    latest = dates[-1]
    start = latest - timedelta(days=days)

    def _window(lo, hi):
        selected = [d for d in dates if lo < d <= hi]
        if not selected:
            return [], 0.0
        earlier = [d for d in dates if d < selected[0]]
        baseline = earlier[-1] if earlier else selected[0]
        calc_span = (selected[-1] - baseline).total_seconds() / 86400.0
        return selected, (max(1.0, float(len(selected))) if calc_span <= 0.0 else calc_span)

    window_dates, span_days = _window(start, latest)
    prior_dates, prior_span_days = _window(start - timedelta(days=days), start)
    return window_dates, span_days, prior_dates, prior_span_days


def rate_of_sale(units, span_days, skus=1, in_stock_days: Optional[float] = None) -> float:
    """Units sold per day (per SKU when skus > 1) over an observed span, adjusted for stockouts (un-censored velocity)."""
    if not skus or skus <= 0:
        return 0.0
    if in_stock_days is not None and float(in_stock_days) > 0:
        effective_span = min(float(span_days or 1.0), max(0.25, float(in_stock_days)))
    else:
        effective_span = float(span_days or 0)
    if effective_span <= 0:
        return 0.0
    return max(0.0, round(float(units or 0) / effective_span / skus, 2))


def _snapshot_timestamp_token(value: Optional[str] = None) -> str:
    parsed = _coerce_iso_timestamp(value)
    if parsed is None:
        parsed = datetime.now().astimezone()
    elif parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat(timespec="seconds")


def _inventory_available_sql(alias: str = "product_sizes") -> str:
    return f"(COALESCE({alias}.available, 0) = 1 OR COALESCE({alias}.inventory_count, 0) > 0)"

def _sql_to_pg(sql: str) -> str:
    """Convert app SQL syntax (? placeholders, case-insensitive LIKE, literal %, MAX(a,b)) to PostgreSQL syntax."""
    if '?' in sql:
        sql = sql.replace('%', '%%')
        sql = sql.replace('?', '%s')
    else:
        sql = re.sub(r'(?<!%)%(?!%)', '%%', sql)

    # Match the case-insensitive behavior expected by existing queries.
    sql = re.sub(r'\bLIKE\b', 'ILIKE', sql)
    # Convert 2-arg MAX(number, expr) to GREATEST(number, expr)
    sql = re.sub(r'\bMAX\s*\(\s*(\d+)\s*,\s*([^)]+)\)', r'GREATEST(\1, \2)', sql, flags=re.IGNORECASE)
    return sql


class _PGRow:
    """Row wrapper around psycopg2 cursor results:
    - Supports integer indexing: row[0], row[1]
    - Supports string key lookup: row['column_name']
    - Supports dict-like .get(key, default)
    - Supports tuple unpacking: a, b = row
    - Iteration yields column values
    - .keys() returns column names
    - .values() returns column values
    - .items() returns (key, value) pairs
    """
    __slots__ = ('_values', '_col_map', '_keys')

    def __init__(self, values_tuple, col_names):
        self._values = values_tuple
        self._keys = col_names
        self._col_map = {}
        for idx, name in enumerate(col_names):
            if name not in self._col_map:
                self._col_map[name] = values_tuple[idx]

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._col_map[key]

    def get(self, key, default=None):
        return self._col_map.get(key, default)

    def __contains__(self, key):
        return key in self._col_map

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._keys

    def values(self):
        return self._values

    def items(self):
        return list(zip(self._keys, self._values))

    def __repr__(self):
        return f"<PGRow {dict(zip(self._keys, self._values))}>"


class _PGCursor:
    """Cursor wrapper around psycopg2 cursor."""
    def __init__(self, pg_cursor):
        self._cur = pg_cursor
        self.lastrowid = None
        self.rowcount = 0

    def _col_names(self):
        return [d[0] for d in self._cur.description] if self._cur.description else []

    def execute(self, sql, params=()):
        self._cur.execute(_sql_to_pg(sql), params)
        self.rowcount = self._cur.rowcount
        return self

    def executemany(self, sql, seq):
        self._cur.executemany(_sql_to_pg(sql), seq)

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return _PGRow(row, self._col_names())

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = self._col_names()
        return [_PGRow(r, cols) for r in rows]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany(size) if size else self._cur.fetchmany()
        if not rows:
            return []
        cols = self._col_names()
        return [_PGRow(r, cols) for r in rows]

    def __iter__(self):
        cols = self._col_names()
        for row in self._cur:
            yield _PGRow(row, cols)

    @property
    def description(self):
        return self._cur.description


class _PGConnection:
    """Connection wrapper around psycopg2 connection with pool recycling support."""
    def __init__(self, pg_conn, pool=None):
        self._conn = pg_conn
        self._conn.autocommit = True
        self._pool = pool

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        pass  # Older call sites may still set this; the wrapper always returns _PGRow.

    def cursor(self):
        return _PGCursor(self._conn.cursor())

    def execute(self, sql, params=()):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql, seq):
        cur = self.cursor()
        cur.executemany(sql, seq)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._pool is not None:
            try:
                self._pool.putconn(self._conn)
            except Exception:
                pass
        else:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self):
        self._conn.autocommit = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.autocommit = True


def _ensure_database_exists(dsn: dict):
    """Create the configured PostgreSQL database if it is missing."""
    if not psycopg2:
        return

    admin_dsn = dict(dsn)
    admin_dsn['dbname'] = 'postgres'

    conn = None
    try:
        conn = psycopg2.connect(**admin_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dsn['dbname'],))
            if cur.fetchone() is None:
                safe_name = dsn['dbname'].replace('"', '""')
                cur.execute(f'CREATE DATABASE "{safe_name}"')
    finally:
        if conn is not None:
            conn.close()


_GLOBAL_PG_POOLS: Dict[str, Any] = {}
_GLOBAL_PG_POOL_LOCK = threading.Lock()


def _get_pg_pool(dsn: dict):
    key = f"{dsn.get('host')}:{dsn.get('port')}:{dsn.get('dbname')}:{dsn.get('user')}"
    if key not in _GLOBAL_PG_POOLS:
        with _GLOBAL_PG_POOL_LOCK:
            if key not in _GLOBAL_PG_POOLS and _PG_AVAILABLE:
                try:
                    from psycopg2.pool import ThreadedConnectionPool
                    _ensure_database_exists(dsn)
                    pool = ThreadedConnectionPool(minconn=1, maxconn=5, **dsn)
                    _GLOBAL_PG_POOLS[key] = pool
                except Exception:
                    _GLOBAL_PG_POOLS[key] = None
    return _GLOBAL_PG_POOLS.get(key)


def _make_pg_connection(dsn: dict) -> '_PGConnection':
    pool = _get_pg_pool(dsn)
    if pool is not None:
        try:
            raw_conn = pool.getconn()
            if raw_conn.closed:
                pool.putconn(raw_conn, close=True)
                raw_conn = pool.getconn()
            return _PGConnection(raw_conn, pool=pool)
        except Exception:
            pass

    try:
        conn = psycopg2.connect(**dsn)
        return _PGConnection(conn)
    except psycopg2.OperationalError as exc:
        msg = str(exc).lower()
        if 'database' not in msg or 'does not exist' not in msg:
            raise
        _ensure_database_exists(dsn)
        conn = psycopg2.connect(**dsn)
        return _PGConnection(conn)


def _get_pg_dsn() -> dict:
    from config import PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DBNAME
    return {
        'host': PG_HOST,
        'port': PG_PORT,
        'user': PG_USER,
        'password': PG_PASSWORD,
        'dbname': PG_DBNAME,
        # Pin the session timezone: queries compare timestamps as text and cut days with ::date,
        # so results must not depend on the server default (EC2's Postgres runs in UTC).
        'options': f"-c timezone={os.getenv('APP_TIMEZONE') or 'Asia/Kolkata'}",
    }


def normalize_fashion_category(raw_cat: str, title: str = "", product_url: str = "", sub_cat: str = "") -> str:
    try:
        from shirts_taxonomy import SHIRTS_PRIMARY_CATEGORIES, classify_shirts
        raw = (raw_cat or "").strip()
        if raw in SHIRTS_PRIMARY_CATEGORIES:
            return raw
        res = classify_shirts(title=title, sub_category=sub_cat)
        return res.get("category") or "Casual Shirts"
    except Exception:
        return "Casual Shirts"

def _load_primary_image_from_json(full_data_json) -> str:
    if not full_data_json:
        return ""
    try:
        data = json.loads(full_data_json) if isinstance(full_data_json, str) else full_data_json
    except Exception:
        return ""
    media = data.get("media", {}) or {}
    return media.get("primary_image") or ""


def _resolve_product_color_fields(product_data: Dict[str, Any]) -> tuple[str, str]:
    product_data = product_data or {}
    product_info = product_data.get("product_info") or {}
    specs = product_data.get("specifications") or {}

    derived_color, derived_hex = extract_primary_color(product_data)
    raw_color_candidates = [
        derived_color,
        product_info.get("primary_color"),
        specs.get("color"),
        specs.get("primary_color"),
        product_data.get("primary_color"),
        product_info.get("base_color"),
    ]

    color_name = None
    for candidate in raw_color_candidates:
        normalized = normalize_color_name(candidate, fallback=None)
        if normalized:
            color_name = normalized
            if normalized != "Multicolor":
                break

    final_color = color_name or "Multicolor"
    final_hex = derive_color_hex(
        final_color,
        raw_hex=derived_hex or product_info.get("color_hex") or specs.get("color_hex") or product_data.get("color_hex"),
        fallback="#0f172a",
    )
    return final_color, final_hex


_PG_DB_INITIALIZED = False


class Database:
    """Thread-safe PostgreSQL database manager for Myntra scraping."""

    def __init__(self, db_path: Optional[Path] = None):
        global _PG_DB_INITIALIZED
        self.db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        skip_db_init = str(os.getenv("SKIP_DB_INIT", "")).strip().lower() in {"1", "true", "yes", "on"}
        if not skip_db_init and not _PG_DB_INITIALIZED:
            self.init_db()

    def get_connection(self):
        """Public thread-local connection accessor & context manager."""
        return self._get_connection()

    def close(self):
        """Cleanly release thread-local connection back to pool."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _get_connection(self):
        """Returns a thread-local PostgreSQL connection."""
        if not _PG_AVAILABLE:
            raise RuntimeError("PostgreSQL driver psycopg2 is required.")

        conn = getattr(self._local, 'conn', None)

        if conn is not None and isinstance(conn, _PGConnection):
            try:
                conn._conn.cursor().execute('SELECT 1')
                return conn
            except Exception:
                try:
                    conn._conn.close()
                except Exception:
                    pass
                self._local.conn = None

        self._local.conn = _make_pg_connection(_get_pg_dsn())
        return self._local.conn

    def init_db(self):
        """Creates PostgreSQL tables and indexes if they do not exist."""
        global _PG_DB_INITIALIZED
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS primary_image_url TEXT;")
            cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS current_total_stock INTEGER DEFAULT 0;")
            cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS current_available_size_count INTEGER DEFAULT 0;")
        except Exception:
            pass
        for stmt in PG_SCHEMA_DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    cur.execute(stmt)
                except Exception as exc:
                    msg = str(exc).lower()
                    # Production imports can race on IF NOT EXISTS index creation.
                    # Ignore benign duplicate-object/index-name conflicts and keep booting.
                    if "already exists" in msg or "pg_class_relname_nsp_index" in msg:
                        continue
                    raise
        self._migrate_snapshot_tables(cur)
        self._migrate_product_cache_columns(cur)
        self._migrate_product_size_inventory_columns(cur)
        self._migrate_brands_table(cur)
        _PG_DB_INITIALIZED = True

    def _migrate_snapshot_tables(self, cur):
        """Upgrade legacy date-only snapshot tables to timestamp granularity for intra-day runs."""
        def _column_type(table_name: str, column_name: str) -> str:
            cur.execute("""
                SELECT data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ? AND column_name = ?;
            """, (table_name, column_name))
            row = cur.fetchone()
            return str(row[0]).lower() if row and row[0] else ""

        snapshot_type = _column_type("daily_inventory_snapshots", "snapshot_date")
        if snapshot_type == "date":
            cur.execute("""
                ALTER TABLE daily_inventory_snapshots
                ALTER COLUMN snapshot_date TYPE TIMESTAMPTZ
                USING (snapshot_date::timestamp AT TIME ZONE current_setting('TIMEZONE'));
            """)

        analytics_type = _column_type("daily_sales_analytics", "analytics_date")
        if analytics_type == "date":
            cur.execute("""
                ALTER TABLE daily_sales_analytics
                ALTER COLUMN analytics_date TYPE TIMESTAMPTZ
                USING (analytics_date::timestamp AT TIME ZONE current_setting('TIMEZONE'));
            """)

    def _migrate_product_cache_columns(self, cur):
        """Add and backfill product-level cache columns used by fast dashboard queries."""
        def _has_column(table_name: str, column_name: str) -> bool:
            cur.execute("""
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ? AND column_name = ?
                LIMIT 1;
            """, (table_name, column_name))
            return cur.fetchone() is not None

        if not _has_column("products", "primary_image_url"):
            cur.execute("ALTER TABLE products ADD COLUMN primary_image_url TEXT;")
        if not _has_column("products", "current_total_stock"):
            cur.execute("ALTER TABLE products ADD COLUMN current_total_stock INTEGER DEFAULT 0;")
        if not _has_column("products", "current_available_size_count"):
            cur.execute("ALTER TABLE products ADD COLUMN current_available_size_count INTEGER DEFAULT 0;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pg_products_total_stock ON products(current_total_stock DESC);")

    def _migrate_product_size_inventory_columns(self, cur):
        """Preserve raw scraped inventory alongside normalized analytics-safe counts."""
        def _has_column(table_name: str, column_name: str) -> bool:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ? AND column_name = ?
                LIMIT 1;
                """,
                (table_name, column_name),
            )
            return cur.fetchone() is not None

        if not _has_column("product_sizes", "raw_inventory_count"):
            cur.execute("ALTER TABLE product_sizes ADD COLUMN raw_inventory_count INTEGER;")
        if not _has_column("product_sizes", "inventory_quality"):
            cur.execute("ALTER TABLE product_sizes ADD COLUMN inventory_quality TEXT;")

    def _migrate_brands_table(self, cur):
        """Keep legacy brand rollups compatible while promoting ethnic wear reporting."""
        def _has_column(table_name: str, column_name: str) -> bool:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ? AND column_name = ?
                LIMIT 1;
                """,
                (table_name, column_name),
            )
            return cur.fetchone() is not None

        if not _has_column("brands", "ethnic_count"):
            cur.execute("ALTER TABLE brands ADD COLUMN ethnic_count INTEGER DEFAULT 0;")

        for legacy_column in ("formal_shoes_count", "activewear_count", "polos_count", "shirts_count", "denims_count", "western_count"):
            if _has_column("brands", legacy_column):
                cur.execute(f"ALTER TABLE brands DROP COLUMN IF EXISTS {legacy_column};")

    def save_product(self, product_data: Dict[str, Any]) -> bool:
        """Saves or updates a product and its sizes in the database."""
        conn = self._get_connection()
        p_info = product_data.get("product_info", {})
        pricing = product_data.get("pricing", {})
        inv_sizes = product_data.get("inventory_and_sizes", {})
        specs = product_data.get("specifications", {})
        ratings = product_data.get("ratings_and_reviews") or product_data.get("ratings") or {}

        product_id = p_info.get("product_id")
        if not product_id:
            return False

        full_json = json.dumps(product_data, ensure_ascii=False)
        now = datetime.now().astimezone().isoformat()

        fit_val = specs.get("toe_shape") or specs.get("fastening") or inv_sizes.get("fit") or specs.get("fit") or ""
        fabric_val = specs.get("upper_material") or specs.get("material") or normalize_fabric(specs.get("fabric") or "") or specs.get("fabric") or ""
        pattern_val = specs.get("pattern") or specs.get("patterns") or ""
        color_val, hex_val = _resolve_product_color_fields(product_data)
        primary_image_url = _extract_primary_image_from_product_data(product_data)

        selling_price = float(pricing.get("selling_price") or p_info.get("selling_price") or product_data.get("price") or 0)
        # Category price band from config.PRICE_MIN/PRICE_MAX. URLs pasted under admin → Sources are
        # kept whatever their price (source_scraper sets enforce_price_band = False).
        if getattr(self, "enforce_price_band", True):
            import config
            if selling_price < float(config.PRICE_MIN) or selling_price > float(config.PRICE_MAX):
                return False

        def _to_str(val, default=""):
            if isinstance(val, dict):
                return str(val.get("typeName") or val.get("name") or val.get("value") or default)
            return str(val) if val is not None else default

        try:
            from shirts_taxonomy import classify_shirts as classify_ethnic
            _cls = classify_ethnic(
                title=_to_str(p_info.get("title")),
                sub_category=_to_str(p_info.get("sub_category")),
                description=_to_str(product_data.get("description")),
                gender=_to_str(p_info.get("gender") or ((product_data.get("analytics_data") or {}).get("analytics") or {}).get("gender")),
                attributes=specs
            )
            clean_cat = _cls.get("category") or normalize_fashion_category(
                _to_str(p_info.get("category")),
                _to_str(p_info.get("title")),
                _to_str(p_info.get("product_url")),
                _to_str(p_info.get("sub_category"))
            )
            clean_sub_cat = _cls.get("sub_category") or _to_str(p_info.get("sub_category")) or "Casual Shirts"
            normalized_size_rows = normalize_inventory_entries(inv_sizes.get("sizes_available", []))
            total_stock, available_size_count = _summarize_inventory_rows(normalized_size_rows)

            with conn:
                conn.execute("""
                    INSERT INTO products (
                        product_id, sku, brand, is_myntra_label, brand_type, title, category, sub_category, gender,
                        product_url, mrp, selling_price, discount_percentage, is_in_stock,
                        fit, fabric, pattern, primary_color, color_hex, average_rating, total_ratings_count, total_reviews_count,
                        primary_image_url, current_total_stock, current_available_size_count,
                        full_data_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(product_id) DO UPDATE SET
                        sku=excluded.sku,
                        brand=excluded.brand,
                        is_myntra_label=excluded.is_myntra_label,
                        brand_type=excluded.brand_type,
                        title=excluded.title,
                        category=excluded.category,
                        sub_category=excluded.sub_category,
                        gender=excluded.gender,
                        product_url=excluded.product_url,
                        mrp=excluded.mrp,
                        selling_price=excluded.selling_price,
                        discount_percentage=excluded.discount_percentage,
                        is_in_stock=excluded.is_in_stock,
                        fit=excluded.fit,
                        fabric=excluded.fabric,
                        pattern=excluded.pattern,
                        primary_color=excluded.primary_color,
                        color_hex=excluded.color_hex,
                        average_rating=excluded.average_rating,
                        total_ratings_count=excluded.total_ratings_count,
                        total_reviews_count=excluded.total_reviews_count,
                        primary_image_url=excluded.primary_image_url,
                        current_total_stock=excluded.current_total_stock,
                        current_available_size_count=excluded.current_available_size_count,
                        full_data_json=excluded.full_data_json,
                        updated_at=excluded.updated_at;
                """, (
                    product_id,
                    _to_str(p_info.get("sku")),
                    _to_str(p_info.get("brand")),
                    1 if p_info.get("is_myntra_label") else 0,
                    _to_str(p_info.get("brand_type")),
                    _to_str(p_info.get("title")),
                    clean_cat,
                    clean_sub_cat,
                    _to_str(p_info.get("gender")),
                    _to_str(p_info.get("product_url")),
                    float(pricing.get("mrp", 0.0) or 0.0),
                    float(pricing.get("selling_price", 0.0) or 0.0),
                    int(pricing.get("discount_percentage", 0) or 0),
                    1 if inv_sizes.get("is_in_stock") else 0,
                    _to_str(fit_val),
                    _to_str(fabric_val),
                    _to_str(pattern_val),
                    _to_str(color_val, "Multicolor"),
                    _to_str(hex_val, "#0f172a"),
                    float(ratings.get("average_rating", 0.0) or 0.0),
                    int(ratings.get("total_ratings_count", 0) or 0),
                    int(ratings.get("total_reviews_count", 0) or 0),
                    primary_image_url,
                    total_stock,
                    available_size_count,
                    full_json,
                    now
                ))

                # Refresh sizes
                conn.execute("DELETE FROM product_sizes WHERE product_id = ?;", (product_id,))
                sizes_to_insert = []
                for s in normalized_size_rows:
                    sizes_to_insert.append((
                        product_id,
                        s.get("size"),
                        s.get("sku_id"),
                        1 if s.get("available") else 0,
                        int(s.get("inventory_count", 0) or 0),
                        int(s.get("raw_inventory_count", 0) or 0),
                        str(s.get("inventory_quality") or "exact")
                    ))
                if sizes_to_insert:
                    conn.executemany("""
                        INSERT INTO product_sizes (product_id, size, sku_id, available, inventory_count, raw_inventory_count, inventory_quality)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                    """, sizes_to_insert)
                conn.execute("""
                    UPDATE products
                    SET current_total_stock = ?, current_available_size_count = ?
                    WHERE product_id = ?;
                """, (total_stock, available_size_count, product_id))

            return True
        except Exception as e:
            print(f"[DB Error] Failed saving product {product_id}: {e}")
            return False

    def save_products_batch(self, products_list: List[Dict[str, Any]]) -> int:
        """Saves multiple products and their sizes in a single high-performance atomic transaction."""
        if not products_list:
            return 0

        conn = self._get_connection()
        now = datetime.now().astimezone().isoformat()

        def _to_str(val, default=""):
            if isinstance(val, dict):
                return str(val.get("typeName") or val.get("name") or val.get("value") or default)
            return str(val) if val is not None else default

        prod_rows = []
        pids = []
        sizes_to_insert = []

        for p_data in products_list:
            p_info = p_data.get("product_info", {})
            pricing = p_data.get("pricing", {})
            inv_sizes = p_data.get("inventory_and_sizes", {})
            specs = p_data.get("specifications", {})
            ratings = p_data.get("ratings_and_reviews") or p_data.get("ratings") or {}

            product_id = p_info.get("product_id")
            if not product_id:
                continue

            fit_val = inv_sizes.get("fit") or specs.get("fit") or ""
            fabric_val = normalize_fabric(specs.get("fabric") or "") or ""
            pattern_val = specs.get("pattern") or ""
            color_val, hex_val = _resolve_product_color_fields(p_data)
            full_json = json.dumps(p_data, ensure_ascii=False)
            primary_image_url = _extract_primary_image_from_product_data(p_data)
            normalized_size_rows = normalize_inventory_entries(inv_sizes.get("sizes_available", []))
            total_stock, available_size_count = _summarize_inventory_rows(normalized_size_rows)

            from shirts_taxonomy import classify_shirts as classify_ethnic
            _cls = classify_ethnic(
                title=_to_str(p_info.get("title")),
                sub_category=_to_str(p_info.get("sub_category")),
                description=_to_str(p_data.get("description")),
                gender=_to_str(p_info.get("gender") or ((p_data.get("analytics_data") or {}).get("analytics") or {}).get("gender")),
                attributes=specs
            )
            clean_cat = _cls.get("category") or normalize_fashion_category(
                _to_str(p_info.get("category")),
                _to_str(p_info.get("title")),
                _to_str(p_info.get("product_url")),
                _to_str(p_info.get("sub_category"))
            )
            clean_sub_cat = _cls.get("sub_category") or _to_str(p_info.get("sub_category")) or "Casual Shirts"

            prod_rows.append((
                product_id,
                _to_str(p_info.get("sku")),
                _to_str(p_info.get("brand")),
                1 if p_info.get("is_myntra_label") else 0,
                _to_str(p_info.get("brand_type")),
                _to_str(p_info.get("title")),
                clean_cat,
                clean_sub_cat,
                _to_str(p_info.get("gender")),
                _to_str(p_info.get("product_url")),
                float(pricing.get("mrp", 0.0) or 0.0),
                float(pricing.get("selling_price", 0.0) or 0.0),
                int(pricing.get("discount_percentage", 0) or 0),
                1 if inv_sizes.get("is_in_stock") else 0,
                _to_str(fit_val),
                _to_str(fabric_val),
                _to_str(pattern_val),
                _to_str(color_val, "Multicolor"),
                _to_str(hex_val, "#0f172a"),
                float(ratings.get("average_rating", 0.0) or 0.0),
                int(ratings.get("total_ratings_count", 0) or 0),
                int(ratings.get("total_reviews_count", 0) or 0),
                primary_image_url,
                total_stock,
                available_size_count,
                full_json,
                now
            ))
            pids.append(product_id)

            for s in normalized_size_rows:
                sizes_to_insert.append((
                    product_id,
                    s.get("size"),
                    s.get("sku_id"),
                    1 if s.get("available") else 0,
                    int(s.get("inventory_count", 0) or 0),
                    int(s.get("raw_inventory_count", 0) or 0),
                    str(s.get("inventory_quality") or "exact")
                ))

        if not prod_rows:
            return 0

        try:
            with self._write_lock:
                conn.execute("BEGIN;")
                conn.executemany("""
                    INSERT INTO products (
                        product_id, sku, brand, is_myntra_label, brand_type, title, category, sub_category, gender,
                        product_url, mrp, selling_price, discount_percentage, is_in_stock,
                        fit, fabric, pattern, primary_color, color_hex, average_rating, total_ratings_count, total_reviews_count,
                        primary_image_url, current_total_stock, current_available_size_count,
                        full_data_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(product_id) DO UPDATE SET
                        sku=excluded.sku,
                        brand=excluded.brand,
                        is_myntra_label=excluded.is_myntra_label,
                        brand_type=excluded.brand_type,
                        title=excluded.title,
                        category=excluded.category,
                        sub_category=excluded.sub_category,
                        gender=excluded.gender,
                        product_url=excluded.product_url,
                        mrp=excluded.mrp,
                        selling_price=excluded.selling_price,
                        discount_percentage=excluded.discount_percentage,
                        is_in_stock=excluded.is_in_stock,
                        fit=excluded.fit,
                        fabric=excluded.fabric,
                        pattern=excluded.pattern,
                        primary_color=excluded.primary_color,
                        color_hex=excluded.color_hex,
                        average_rating=excluded.average_rating,
                        total_ratings_count=excluded.total_ratings_count,
                        total_reviews_count=excluded.total_reviews_count,
                        primary_image_url=excluded.primary_image_url,
                        current_total_stock=excluded.current_total_stock,
                        current_available_size_count=excluded.current_available_size_count,
                        full_data_json=excluded.full_data_json,
                        updated_at=excluded.updated_at;
                """, prod_rows)

                conn.executemany("DELETE FROM product_sizes WHERE product_id = ?;", [(pid,) for pid in pids])

                if sizes_to_insert:
                    conn.executemany("""
                        INSERT INTO product_sizes (product_id, size, sku_id, available, inventory_count, raw_inventory_count, inventory_quality)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                    """, sizes_to_insert)

                conn.execute("COMMIT;")
            return len(prod_rows)
        except Exception as e:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            print(f"[DB Error] Batch save failed for {len(prod_rows)} products: {e}")
            return 0

    def backfill_product_colors(self, batch_size: int = 1000) -> Dict[str, int]:
        """Repair stored color fields from full_data_json without re-scraping products."""
        conn = self._get_connection()
        cur = conn.cursor()
        scanned = 0
        updated = 0
        skipped = 0

        cur.execute("""
            SELECT product_id, full_data_json, primary_color, color_hex
            FROM products
            ORDER BY product_id ASC;
        """)

        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break

            updates = []
            for row in rows:
                scanned += 1
                try:
                    full_data = json.loads(row["full_data_json"]) if row["full_data_json"] else {}
                except Exception:
                    skipped += 1
                    continue

                color_name, color_hex = _resolve_product_color_fields(full_data)
                current_color_raw = str(row["primary_color"] or "").strip()
                current_color = normalize_color_name(current_color_raw, fallback="Multicolor") or "Multicolor"
                current_hex_raw = str(row["color_hex"] or "").strip().lower()
                current_hex = derive_color_hex(current_color, raw_hex=row["color_hex"], fallback="#0f172a")

                if (
                    color_name == current_color
                    and color_hex == current_hex
                    and current_color_raw == color_name
                    and current_hex_raw == color_hex.lower()
                ):
                    continue

                updates.append((color_name, color_hex, row["product_id"]))

            if not updates:
                continue

            try:
                with self._write_lock:
                    conn.execute("BEGIN;")
                    conn.executemany(
                        """
                        UPDATE products
                        SET primary_color = ?, color_hex = ?
                        WHERE product_id = ?;
                        """,
                        updates,
                    )
                    conn.execute("COMMIT;")
                updated += len(updates)
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise

        return {
            "scanned": scanned,
            "updated": updated,
            "skipped": skipped,
        }

    def backfill_inventory_counts(self, batch_size: int = 5000) -> Dict[str, int]:
        """Repair stored size inventory counts using the latest product-level sentinel rules."""
        conn = self._get_connection()
        cur = conn.cursor()
        payload_cur = conn.cursor()
        scanned_rows = 0
        updated_rows = 0
        scanned_products = 0
        updated_products = 0

        cur.execute("""
            SELECT id, product_id, size, sku_id, available, inventory_count, raw_inventory_count, inventory_quality
            FROM product_sizes
            ORDER BY product_id ASC, id ASC;
        """)

        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break

            size_updates = []
            touched_products = set()
            rows_by_product: Dict[int, List[Any]] = {}

            for row in rows:
                scanned_rows += 1
                product_id = int(row["product_id"])
                rows_by_product.setdefault(product_id, []).append(row)

            product_payload_rows: Dict[int, Any] = {}
            product_ids_for_chunk = sorted(rows_by_product.keys())
            if product_ids_for_chunk:
                placeholders = ",".join("?" for _ in product_ids_for_chunk)
                payload_cur.execute(
                    f"SELECT product_id, full_data_json FROM products WHERE product_id IN ({placeholders});",
                    product_ids_for_chunk,
                )
                for payload_row in payload_cur.fetchall():
                    product_payload_rows[int(payload_row["product_id"])] = payload_row["full_data_json"]

            for product_id, product_rows in rows_by_product.items():
                # Shopify-source listings report availability only (1 per in-stock size, tagged
                # 'availability_only'); re-normalizing would turn them back into real-looking stock.
                if any(str(r.get("inventory_quality") or "") == "availability_only" for r in product_rows):
                    continue
                scanned_products += 1
                payload_json = product_payload_rows.get(product_id)
                payload = _safe_parse_json(payload_json, default={}) if payload_json else {}
                payload_sizes = ((payload.get("inventory_and_sizes") or {}).get("sizes_available") or []) if isinstance(payload, dict) else []

                payload_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
                for payload_size in payload_sizes:
                    if not isinstance(payload_size, dict):
                        continue
                    size_key = str(payload_size.get("size") or "").strip()
                    sku_key = str(payload_size.get("sku_id") or "").strip()
                    payload_by_key[(size_key, sku_key)] = payload_size
                    if size_key and (size_key, "") not in payload_by_key:
                        payload_by_key[(size_key, "")] = payload_size

                prepared_entries = []
                payload_meta_by_row_id: Dict[int, Dict[str, Any]] = {}
                for row in product_rows:
                    payload_size = payload_by_key.get((str(row["size"] or "").strip(), str(row["sku_id"] or "").strip())) or payload_by_key.get((str(row["size"] or "").strip(), ""))
                    payload_available = bool(payload_size.get("available")) if isinstance(payload_size, dict) else bool(row["available"])
                    payload_inventory_count = int((payload_size or {}).get("inventory_count") or 0) if isinstance(payload_size, dict) else int(row["inventory_count"] or 0)
                    payload_raw_count = None
                    if isinstance(payload_size, dict) and payload_size.get("raw_inventory_count") is not None:
                        payload_raw_count = int(payload_size.get("raw_inventory_count") or 0)

                    current_count = int(row["inventory_count"] or 0)
                    current_raw = row.get("raw_inventory_count")
                    has_corrupted_zero_raw = current_raw == 0 and current_count == 0 and payload_inventory_count > 0
                    use_payload_source = current_raw is None or has_corrupted_zero_raw or bool(row["available"]) != payload_available

                    source_available = payload_available if use_payload_source else bool(row["available"])
                    source_inventory_count = payload_raw_count if payload_raw_count is not None else (payload_inventory_count if use_payload_source else current_raw if current_raw is not None else current_count)
                    source_raw_count = payload_raw_count if payload_raw_count is not None else (current_raw if (current_raw is not None and not has_corrupted_zero_raw) else None)

                    prepared_entries.append({
                        "size": row["size"],
                        "sku_id": row["sku_id"],
                        "available": source_available,
                        "inventory_count": source_inventory_count,
                        "raw_inventory_count": source_raw_count,
                    })
                    payload_meta_by_row_id[int(row["id"])] = {
                        "payload_size": payload_size,
                        "source_had_raw": source_raw_count is not None,
                    }

                normalized_entries = normalize_inventory_entries(prepared_entries)

                product_changed = False
                for row, normalized in zip(product_rows, normalized_entries):
                    current_count = int(row["inventory_count"] or 0)
                    current_available = 1 if bool(row["available"]) else 0
                    normalized_count = int(normalized.get("inventory_count", 0) or 0)
                    current_raw = row.get("raw_inventory_count")
                    current_quality = str(row.get("inventory_quality") or "")
                    normalized_available = 1 if bool(normalized.get("available")) else 0
                    normalized_raw = int(normalized.get("raw_inventory_count", 0) or 0) if normalized.get("raw_inventory_count") is not None else None
                    normalized_quality = str(normalized.get("inventory_quality") or "exact")
                    payload_meta = payload_meta_by_row_id.get(int(row["id"]), {})
                    if not payload_meta.get("source_had_raw"):
                        normalized_quality = "legacy_unknown" if normalized_available else "unavailable"
                        normalized_raw = None
                        if normalized_available and normalized_count >= 500:
                            normalized_count = 1
                            normalized_quality = "legacy_modeled_high"
                    needs_available_update = normalized_available != current_available
                    needs_raw_backfill = current_raw != normalized_raw
                    needs_quality_backfill = current_quality != normalized_quality
                    if normalized_count == current_count and not needs_available_update and not needs_raw_backfill and not needs_quality_backfill:
                        continue
                    size_updates.append((normalized_available, normalized_count, normalized_raw, normalized_quality, row["id"]))
                    product_changed = True

                if product_changed:
                    touched_products.add(product_id)
                    updated_products += 1

            if not size_updates:
                continue

            try:
                with self._write_lock:
                    conn.execute("BEGIN;")
                    conn.executemany(
                        """
                        UPDATE product_sizes
                        SET available = ?,
                            inventory_count = ?,
                            raw_inventory_count = ?,
                            inventory_quality = ?
                        WHERE id = ?;
                        """,
                        size_updates,
                    )
                    if touched_products:
                        product_ids = sorted(touched_products)
                        placeholders = ",".join("?" for _ in product_ids)
                        conn.execute(
                            f"""
                            WITH agg AS (
                                SELECT
                                    product_id,
                                    COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(inventory_count, 0) > 0 THEN inventory_count ELSE 0 END), 0) AS total_stock,
                                    COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(inventory_count, 0) > 0 THEN 1 ELSE 0 END), 0) AS available_size_count
                                FROM product_sizes
                                WHERE product_id IN ({placeholders})
                                GROUP BY product_id
                            )
                            UPDATE products p
                            SET current_total_stock = COALESCE(agg.total_stock, 0),
                                current_available_size_count = COALESCE(agg.available_size_count, 0),
                                updated_at = NOW()
                            FROM agg
                            WHERE p.product_id = agg.product_id;
                            """,
                            product_ids,
                        )
                    conn.execute("COMMIT;")
                updated_rows += len(size_updates)
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise

        return {
            "scanned_rows": scanned_rows,
            "updated_rows": updated_rows,
            "scanned_products": scanned_products,
            "updated_products": updated_products,
        }

    def refresh_product_cache_fields(self, batch_size: int = 5000) -> Dict[str, int]:
        """Backfill product-level cache columns from current size rows and stored JSON."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM products;")
        scanned_products = int((cur.fetchone() or [0])[0] or 0)
        updated_stock_rows = 0
        last_product_id = 0
        while True:
            cur.execute("""
                SELECT product_id
                FROM products
                WHERE product_id > ?
                ORDER BY product_id ASC
                LIMIT ?;
            """, (last_product_id, batch_size))
            product_rows = cur.fetchall()
            if not product_rows:
                break
            product_ids = [int(row[0]) for row in product_rows if row and row[0]]
            if not product_ids:
                break
            last_product_id = product_ids[-1]
            placeholders = ",".join("?" for _ in product_ids)
            with self._write_lock:
                conn.execute("BEGIN;")
                conn.execute(
                    f"""
                    WITH target_products AS (
                        SELECT UNNEST(ARRAY[{placeholders}])::BIGINT AS product_id
                    ),
                    agg AS (
                        SELECT
                            tp.product_id,
                            COALESCE(SUM(CASE WHEN COALESCE(s.available, 0) = 1 AND COALESCE(s.inventory_count, 0) > 0 THEN s.inventory_count ELSE 0 END), 0) AS total_stock,
                            COALESCE(SUM(CASE WHEN COALESCE(s.available, 0) = 1 AND COALESCE(s.inventory_count, 0) > 0 THEN 1 ELSE 0 END), 0) AS available_size_count
                        FROM target_products tp
                        LEFT JOIN product_sizes s ON s.product_id = tp.product_id
                        GROUP BY tp.product_id
                    )
                    UPDATE products p
                    SET current_total_stock = COALESCE(agg.total_stock, 0),
                        current_available_size_count = COALESCE(agg.available_size_count, 0)
                    FROM agg
                    WHERE p.product_id = agg.product_id;
                    """,
                    product_ids,
                )
                conn.execute("COMMIT;")
            updated_stock_rows += len(product_ids)

        scanned_images = 0
        updated_images = 0
        cur.execute("""
            SELECT product_id, full_data_json, primary_image_url
            FROM products
            WHERE COALESCE(primary_image_url, '') = ''
            ORDER BY product_id ASC;
        """)
        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            updates = []
            for row in rows:
                scanned_images += 1
                primary = _load_primary_image_from_json(row["full_data_json"])
                if not primary:
                    continue
                updates.append((primary, row["product_id"]))
            if not updates:
                continue
            with self._write_lock:
                conn.execute("BEGIN;")
                conn.executemany("""
                    UPDATE products
                    SET primary_image_url = ?
                    WHERE product_id = ?;
                """, updates)
                conn.execute("COMMIT;")
            updated_images += len(updates)

        return {
            "scanned_products": scanned_products,
            "updated_stock_rows": updated_stock_rows,
            "updated_images": updated_images,
            "scanned_images": scanned_images,
        }

    def is_product_scraped(self, product_id: int) -> bool:
        """Checks if a product has already been scraped."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM products WHERE product_id = ? LIMIT 1;", (product_id,))
        row = cur.fetchone()
        return row is not None

    def get_scraped_product_ids(self) -> Set[int]:
        """Returns the set of all product IDs already present in the database."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT product_id FROM products;")
        return {row[0] for row in cur.fetchall()}

    def get_brand_product_count(self, brand: str) -> int:
        """Returns the number of distinct stored products for a brand."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(DISTINCT product_id) FROM products WHERE brand = ?;",
            (brand,),
        )
        row = cur.fetchone()
        return int(row[0] or 0) if row else 0

    def save_crawl_state(self, category: str, brand: str, page: int, items_scraped: int, status: str = "IN_PROGRESS"):
        """Saves current crawling checkpoint for resuming."""
        conn = self._get_connection()
        now = datetime.now().astimezone().isoformat()
        try:
            with self._write_lock:
                conn.execute("BEGIN;")
                conn.execute("""
                    INSERT INTO crawl_state (category, brand, page, items_scraped, status, last_scraped_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category, brand) DO UPDATE SET
                        page=excluded.page,
                        items_scraped=excluded.items_scraped,
                        status=excluded.status,
                        last_scraped_at=excluded.last_scraped_at;
                """, (category, brand, page, items_scraped, status, now))
                conn.execute("COMMIT;")
        except Exception as e:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            print(f"[DB Error] Failed updating crawl state ({category}, {brand}): {e}")

    def get_crawl_state(self, category: str, brand: str) -> Optional[Dict[str, Any]]:
        """Retrieves checkpoint state for given category and brand."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT category, brand, page, items_scraped, status, last_scraped_at FROM crawl_state WHERE category = ? AND brand = ?;",
            (category, brand)
        )
        row = cur.fetchone()
        if row:
            return dict(row)
        return None

    def audit_catalog_integrity(self) -> Dict[str, Any]:
        """Run a compact data-integrity audit across product, price, stock, color, fabric, and size data."""
        conn = self._get_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) AS total_products FROM products;")
        total_products = int((cur.fetchone() or [0])[0] or 0)

        cur.execute("SELECT COUNT(*) AS total_size_rows FROM product_sizes;")
        total_size_rows = int((cur.fetchone() or [0])[0] or 0)

        cur.execute("""
            WITH size_agg AS (
                SELECT
                    product_id,
                    COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(inventory_count, 0) > 0 THEN inventory_count ELSE 0 END), 0) AS summed_stock,
                    COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(inventory_count, 0) > 0 THEN 1 ELSE 0 END), 0) AS available_sizes
                FROM product_sizes
                GROUP BY product_id
            )
            SELECT
                COUNT(*) AS mismatched_products,
                COALESCE(SUM(ABS(COALESCE(p.current_total_stock, 0) - COALESCE(sa.summed_stock, 0))), 0) AS abs_unit_gap,
                COALESCE(SUM(CASE WHEN COALESCE(p.current_available_size_count, 0) != COALESCE(sa.available_sizes, 0) THEN 1 ELSE 0 END), 0) AS size_count_mismatch_products,
                COALESCE(SUM(CASE WHEN COALESCE(p.is_in_stock, 0) = 1 AND COALESCE(sa.summed_stock, 0) <= 0 THEN 1 ELSE 0 END), 0) AS flagged_in_stock_but_zero,
                COALESCE(SUM(CASE WHEN COALESCE(p.is_in_stock, 0) = 0 AND COALESCE(sa.summed_stock, 0) > 0 THEN 1 ELSE 0 END), 0) AS flagged_oos_but_positive
            FROM products p
            LEFT JOIN size_agg sa ON sa.product_id = p.product_id
            WHERE COALESCE(p.current_total_stock, 0) != COALESCE(sa.summed_stock, 0)
               OR COALESCE(p.current_available_size_count, 0) != COALESCE(sa.available_sizes, 0)
               OR (COALESCE(p.is_in_stock, 0) = 1 AND COALESCE(sa.summed_stock, 0) <= 0)
               OR (COALESCE(p.is_in_stock, 0) = 0 AND COALESCE(sa.summed_stock, 0) > 0);
        """)
        stock_row = cur.fetchone() or {}

        cur.execute("""
            SELECT
                COUNT(*) AS invalid_price_rows,
                COALESCE(SUM(CASE WHEN COALESCE(selling_price, 0) <= 0 THEN 1 ELSE 0 END), 0) AS non_positive_selling_price,
                COALESCE(SUM(CASE WHEN COALESCE(mrp, 0) <= 0 THEN 1 ELSE 0 END), 0) AS non_positive_mrp,
                COALESCE(SUM(CASE WHEN COALESCE(selling_price, 0) > COALESCE(mrp, 0) AND COALESCE(mrp, 0) > 0 THEN 1 ELSE 0 END), 0) AS selling_gt_mrp,
                COALESCE(SUM(CASE WHEN COALESCE(discount_percentage, 0) < 0 OR COALESCE(discount_percentage, 0) > 100 THEN 1 ELSE 0 END), 0) AS invalid_discount_pct
            FROM products
            WHERE COALESCE(selling_price, 0) <= 0
               OR COALESCE(mrp, 0) <= 0
               OR (COALESCE(selling_price, 0) > COALESCE(mrp, 0) AND COALESCE(mrp, 0) > 0)
               OR COALESCE(discount_percentage, 0) < 0
               OR COALESCE(discount_percentage, 0) > 100;
        """)
        price_row = cur.fetchone() or {}

        cur.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN COALESCE(primary_color, '') = '' OR LOWER(TRIM(primary_color)) IN ('unknown','na','n/a','null','none','nil','-','--') THEN 1 ELSE 0 END), 0) AS invalid_color_products,
                COALESCE(SUM(CASE WHEN COALESCE(color_hex, '') !~ '^#[0-9a-fA-F]{6}$' THEN 1 ELSE 0 END), 0) AS invalid_color_hex_products,
                COALESCE(SUM(CASE WHEN COALESCE(fabric, '') = '' THEN 1 ELSE 0 END), 0) AS missing_fabric_products,
                COALESCE(SUM(CASE WHEN COALESCE(primary_image_url, '') = '' THEN 1 ELSE 0 END), 0) AS missing_primary_image_products
            FROM products;
        """)
        attr_row = cur.fetchone() or {}

        cur.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN COALESCE(size, '') = '' THEN 1 ELSE 0 END), 0) AS blank_size_rows,
                COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(inventory_count, 0) <= 0 THEN 1 ELSE 0 END), 0) AS available_with_zero_rows,
                COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 0 AND COALESCE(inventory_count, 0) > 0 THEN 1 ELSE 0 END), 0) AS unavailable_with_positive_rows,
                COALESCE(SUM(CASE WHEN COALESCE(inventory_count, 0) < 0 THEN 1 ELSE 0 END), 0) AS negative_inventory_rows,
                COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(inventory_count, 0) >= 500 THEN 1 ELSE 0 END), 0) AS suspicious_high_inventory_rows,
                COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(raw_inventory_count, 0) > 0 THEN 1 ELSE 0 END), 0) AS raw_inventory_rows,
                COALESCE(SUM(CASE WHEN raw_inventory_count IS NOT NULL AND COALESCE(available, 0) = 1 AND COALESCE(raw_inventory_count, 0) != COALESCE(inventory_count, 0) THEN 1 ELSE 0 END), 0) AS normalized_placeholder_rows,
                COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(inventory_quality, '') = 'exact' THEN 1 ELSE 0 END), 0) AS exact_inventory_rows,
                COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(inventory_quality, '') IN ('placeholder_cluster', 'placeholder_family', 'placeholder_exact_sentinel', 'legacy_modeled_high') THEN 1 ELSE 0 END), 0) AS placeholder_inventory_rows,
                COALESCE(SUM(CASE WHEN COALESCE(inventory_quality, '') = '' OR inventory_quality IS NULL THEN 1 ELSE 0 END), 0) AS unknown_inventory_quality_rows
            FROM product_sizes;
        """)
        size_row = cur.fetchone() or {}

        cur.execute("""
            SELECT COUNT(*) AS missing_size_products
            FROM products p
            WHERE NOT EXISTS (
                SELECT 1
                FROM product_sizes s
                WHERE s.product_id = p.product_id
            );
        """)
        missing_size_products = int((cur.fetchone() or [0])[0] or 0)

        cur.execute("""
            SELECT COUNT(*) AS missing_target_brands
            FROM (
                SELECT DISTINCT brand FROM products
            ) b;
        """)

        issue_counts = {
            "stock_cache_mismatch_products": int(stock_row.get("mismatched_products") or 0),
            "stock_cache_abs_unit_gap": int(stock_row.get("abs_unit_gap") or 0),
            "stock_cache_size_count_mismatch_products": int(stock_row.get("size_count_mismatch_products") or 0),
            "flagged_in_stock_but_zero_products": int(stock_row.get("flagged_in_stock_but_zero") or 0),
            "flagged_oos_but_positive_products": int(stock_row.get("flagged_oos_but_positive") or 0),
            "invalid_price_rows": int(price_row.get("invalid_price_rows") or 0),
            "non_positive_selling_price": int(price_row.get("non_positive_selling_price") or 0),
            "non_positive_mrp": int(price_row.get("non_positive_mrp") or 0),
            "selling_gt_mrp": int(price_row.get("selling_gt_mrp") or 0),
            "invalid_discount_pct": int(price_row.get("invalid_discount_pct") or 0),
            "invalid_color_products": int(attr_row.get("invalid_color_products") or 0),
            "invalid_color_hex_products": int(attr_row.get("invalid_color_hex_products") or 0),
            "missing_fabric_products": int(attr_row.get("missing_fabric_products") or 0),
            "missing_primary_image_products": int(attr_row.get("missing_primary_image_products") or 0),
            "blank_size_rows": int(size_row.get("blank_size_rows") or 0),
            "available_with_zero_rows": int(size_row.get("available_with_zero_rows") or 0),
            "unavailable_with_positive_rows": int(size_row.get("unavailable_with_positive_rows") or 0),
            "negative_inventory_rows": int(size_row.get("negative_inventory_rows") or 0),
            "suspicious_high_inventory_rows": int(size_row.get("suspicious_high_inventory_rows") or 0),
            "raw_inventory_rows": int(size_row.get("raw_inventory_rows") or 0),
            "exact_inventory_rows": int(size_row.get("exact_inventory_rows") or 0),
            "placeholder_inventory_rows": int(size_row.get("placeholder_inventory_rows") or 0),
            "normalized_placeholder_rows": int(size_row.get("normalized_placeholder_rows") or 0),
            "unknown_inventory_quality_rows": int(size_row.get("unknown_inventory_quality_rows") or 0),
            "missing_size_products": missing_size_products,
        }

        blocking_issue_total = sum([
            issue_counts["stock_cache_mismatch_products"],
            issue_counts["flagged_in_stock_but_zero_products"],
            issue_counts["flagged_oos_but_positive_products"],
            issue_counts["invalid_price_rows"],
            issue_counts["available_with_zero_rows"],
            issue_counts["unavailable_with_positive_rows"],
            issue_counts["negative_inventory_rows"],
            issue_counts["suspicious_high_inventory_rows"],
        ])

        coverage = {
            "total_products": total_products,
            "total_size_rows": total_size_rows,
            "fabric_fill_pct": round(((total_products - issue_counts["missing_fabric_products"]) / max(1, total_products)) * 100, 2),
            "image_fill_pct": round(((total_products - issue_counts["missing_primary_image_products"]) / max(1, total_products)) * 100, 2),
            "color_fill_pct": round(((total_products - issue_counts["invalid_color_products"]) / max(1, total_products)) * 100, 2),
        }

        return {
            "status": "PASS" if blocking_issue_total == 0 else "REVIEW",
            "blocking_issue_total": blocking_issue_total,
            "coverage": coverage,
            "issues": issue_counts,
            "recommended_next_step": (
                "No blocking catalog integrity issues detected."
                if blocking_issue_total == 0
                else "Run inventory cache refresh and review suspicious normalized inventory rows before trusting dashboard totals."
            ),
        }

    def get_stats(self) -> Dict[str, Any]:
        """Returns high-level statistics of stored products with single-pass aggregation."""
        conn = self._get_connection()
        cur = conn.cursor()
        
        # Prefer the curated brands table, but fall back to live catalog rollups when it is empty.
        cur.execute("""
            SELECT 
                COUNT(*) as total_brands,
                SUM(CASE WHEN is_myntra_label = 1 THEN 1 ELSE 0 END) as myntra_brands
            FROM brands;
        """)
        b_row = cur.fetchone()
        total_brands = int(b_row[0] or 0)
        myntra_brands_count = int(b_row[1] or 0)
        if total_brands == 0:
            cur.execute("""
                SELECT
                    COUNT(DISTINCT brand) AS total_brands,
                    COUNT(DISTINCT CASE WHEN is_myntra_label = 1 THEN brand END) AS myntra_brands
                FROM products
                WHERE brand IS NOT NULL AND TRIM(brand) <> '';
            """)
            fallback_brand_row = cur.fetchone()
            total_brands = int(fallback_brand_row[0] or 0)
            myntra_brands_count = int(fallback_brand_row[1] or 0)
        non_myntra_brands_count = max(0, total_brands - myntra_brands_count)

        # Covering index query on idx_products_fast_kpi
        cur.execute("""
            SELECT 
                COUNT(*) as total_products,
                SUM(CASE WHEN is_in_stock = 1 THEN 1 ELSE 0 END) as in_stock,
                ROUND(AVG(selling_price), 2) as avg_price,
                ROUND(AVG(discount_percentage), 1) as avg_discount
            FROM products;
        """)
        row = cur.fetchone()
        total_products = int(row[0] or 0)
        in_stock = int(row[1] or 0)
        avg_price = float(row[2] or 0.0)
        avg_discount = float(row[3] or 0.0)

        # Index query on idx_products_rating
        cur.execute("SELECT ROUND(AVG(average_rating), 2) FROM products WHERE average_rating > 0;")
        avg_rating_row = cur.fetchone()
        avg_rating = float(avg_rating_row[0] or 0.0) if avg_rating_row else 0.0

        # Index query on idx_products_mylabel
        cur.execute("SELECT COUNT(*) FROM products WHERE is_myntra_label = 1;")
        myntra_label_products = int(cur.fetchone()[0] or 0)
        non_myntra_products = max(0, total_products - myntra_label_products)

        cur.execute("SELECT category, COUNT(*) FROM products GROUP BY category;")
        by_category = dict(cur.fetchall())

        cur.execute("SELECT gender, COUNT(*) FROM products GROUP BY gender;")
        by_gender = dict(cur.fetchall())

        return {
            "total_products": total_products,
            "total_brands": total_brands,
            "myntra_label_products": myntra_label_products,
            "non_myntra_products": non_myntra_products,
            "myntra_brands_count": myntra_brands_count,
            "non_myntra_brands_count": non_myntra_brands_count,
            "in_stock": in_stock,
            "out_of_stock": total_products - in_stock,
            "categories": by_category,
            "genders": by_gender,
            "average_rating": avg_rating,
            "average_price": avg_price,
            "average_discount": avg_discount
        }

    def iterate_all_products(self):
        """Generator yielding full product dictionaries from database."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT full_data_json FROM products ORDER BY product_id;")
        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                try:
                    yield json.loads(row[0])
                except (json.JSONDecodeError, ValueError):
                    try:
                        yield ast.literal_eval(row[0])
                    except Exception:
                        continue
                except Exception:
                    continue

    # Size rows whose stored count is a normalized marketplace placeholder; a large
    # stock delta on these products is a normalization artifact, not a sale/restock.
    PLACEHOLDER_INVENTORY_QUALITIES = (
        'placeholder_cluster', 'placeholder_family', 'placeholder_exact_sentinel', 'legacy_modeled_high',
    )

    def dedupe_shared_skus(self, cur=None) -> int:
        """Count each Myntra sku_id's stock once.

        Myntra can list the same physical SKUs under several style ids (colour or
        set variants that share one stock pool). Storing the pool on every listing
        multiplies stock, and every drop in the pool is booked as a sale once per
        listing. The lowest product_id keeps the stock; the other listings' rows are
        zeroed and tagged 'shared_sku_duplicate' (raw_inventory_count keeps the
        observed value). Returns the number of size rows changed.
        """
        if cur is None:
            cur = self._get_connection().cursor()
        cur.execute("""
            WITH owners AS (
                SELECT sku_id, MIN(product_id) AS owner_id
                FROM product_sizes
                WHERE sku_id IS NOT NULL
                GROUP BY sku_id
                HAVING COUNT(DISTINCT product_id) > 1
            )
            UPDATE product_sizes s
            SET inventory_count = 0,
                raw_inventory_count = COALESCE(s.raw_inventory_count, s.inventory_count),
                inventory_quality = 'shared_sku_duplicate'
            FROM owners o
            WHERE s.sku_id = o.sku_id
              AND s.product_id <> o.owner_id
              AND (COALESCE(s.inventory_count, 0) <> 0 OR COALESCE(s.inventory_quality, '') <> 'shared_sku_duplicate')
            RETURNING s.product_id;
        """)
        touched = sorted({row[0] for row in cur.fetchall()})
        if touched:
            placeholders = ",".join("?" for _ in touched)
            cur.execute(f"""
                WITH agg AS (
                    SELECT
                        product_id,
                        COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND COALESCE(inventory_count, 0) > 0 THEN inventory_count ELSE 0 END), 0) AS total_stock,
                        COALESCE(SUM(CASE WHEN COALESCE(available, 0) = 1 AND (COALESCE(inventory_count, 0) > 0 OR inventory_quality = 'shared_sku_duplicate') THEN 1 ELSE 0 END), 0) AS available_size_count
                    FROM product_sizes
                    WHERE product_id IN ({placeholders})
                    GROUP BY product_id
                )
                UPDATE products p
                SET current_total_stock = agg.total_stock,
                    current_available_size_count = agg.available_size_count
                FROM agg
                WHERE p.product_id = agg.product_id;
            """, touched)
        return len(touched)

    def _placeholder_product_ids(self, cur) -> Set[int]:
        quality_placeholders = ",".join("?" for _ in self.PLACEHOLDER_INVENTORY_QUALITIES)
        cur.execute(
            f"SELECT DISTINCT product_id FROM product_sizes WHERE inventory_quality IN ({quality_placeholders});",
            self.PLACEHOLDER_INVENTORY_QUALITIES,
        )
        return {row[0] for row in cur.fetchall()}

    def _fully_shared_product_ids(self, cur) -> Set[int]:
        """Listings whose stock movement must not be read as sales or restocks: every available size
        is a shared_sku_duplicate (stock owned elsewhere), or the listing only reports availability
        (Shopify sources, sizes tagged 'availability_only' — counts are 1 per in-stock size)."""
        cur.execute("""
            SELECT product_id
            FROM product_sizes
            WHERE COALESCE(available, 0) = 1
            GROUP BY product_id
            HAVING BOOL_AND(inventory_quality = 'shared_sku_duplicate')
            UNION
            SELECT DISTINCT product_id FROM product_sizes WHERE inventory_quality = 'availability_only';
        """)
        return {r[0] for r in cur.fetchall()}

    @staticmethod
    def _build_sales_records(snapshot_token, snapshot_dt, prev_snapshot_dt, current_rows, prev_map,
                             placeholder_product_ids, shared_product_ids=frozenset(),
                             prev_sizes=None, cur_sizes=None, rules=None, prev_pool=None, cur_pool=None):
        """Derive one daily_sales_analytics row per product from two consecutive snapshots.

        current_rows: (product_id, brand, category, selling_price, mrp, discount_percentage,
        is_in_stock, total_stock). prev_map: product_id -> (total_stock, selling_price), or
        None for the first (baseline) snapshot. shared_product_ids: listings whose stock is
        owned by another listing; they never book movement themselves.
        prev_sizes / cur_sizes: product_id -> {size: (available, count)} at the previous and
        current snapshot, when size-level snapshots exist for both; otherwise None.
        Stock that falls is split into units sold and unverified units (see sales_rules()).
        Returns (records, units, revenue, stock_added).
        """
        rules = rules or sales_rules()
        records = []
        total_units_sold = 0
        total_revenue = 0.0
        total_stock_added = 0

        if prev_map is None:
            for r in current_rows:
                pid, brand, cat, cur_price, mrp, disc, in_stock, cur_stock = r
                records.append((
                    snapshot_token, pid, brand, cat, 0, 0.0, 0, 0.0, 0.0,
                    "HEALTHY" if in_stock else "OOS", 0
                ))
            return records, 0, 0.0, 0

        gap_seconds = max((snapshot_dt - (prev_snapshot_dt or snapshot_dt)).total_seconds(), 60.0)
        days_gap = max(gap_seconds / 86400.0, 1.0)
        size_aware = prev_sizes is not None and cur_sizes is not None

        for r in current_rows:
            pid, brand, cat, cur_price, mrp, disc, in_stock, cur_stock = r
            cur_price = float(cur_price or 0)
            cur_stock = int(cur_stock or 0)
            prev_stock, prev_price = prev_map.get(pid, (cur_stock, cur_price))
            prev_stock = int(prev_stock or 0)
            prev_price = float(prev_price or 0)

            units_sold = 0
            unverified = 0
            stock_added = 0
            data_unreliable = False
            price_delta = round(cur_price - prev_price, 2)
            effective_price = cur_price if cur_price > 0 else prev_price

            if pid in shared_product_ids:
                pass
            elif cur_stock < prev_stock:
                raw_diff = prev_stock - cur_stock
                if raw_diff > rules["placeholder_max_drop"] and pid in placeholder_product_ids:
                    # Old-vs-normalized placeholder count artifact, not organic movement.
                    data_unreliable = True
                else:
                    pulled = 0
                    if size_aware and pid in prev_sizes:
                        before, after = prev_sizes.get(pid, {}), cur_sizes.get(pid, {})
                        for size, (was_available, count) in before.items():
                            now_available = after.get(size, (0, 0))[0]
                            if was_available and not now_available and count >= rules["size_pull_min"]:
                                pulled += count
                        pulled = min(pulled, raw_diff)
                        remaining = raw_diff - pulled
                        suspicious = remaining / days_gap > rules["max_units_per_day"]
                    else:
                        remaining = raw_diff
                        collapsed = (prev_stock >= rules["collapse_min_stock"]
                                     and cur_stock <= prev_stock * (1 - rules["collapse_share"]))
                        suspicious = collapsed or remaining / days_gap > rules["max_units_per_day"]
                    if suspicious:
                        unverified = raw_diff
                    else:
                        unverified = pulled
                        units_sold = remaining
                        total_units_sold += units_sold
                        total_revenue += units_sold * effective_price
            elif cur_stock > prev_stock:
                stock_added = cur_stock - prev_stock
                total_stock_added += stock_added

            # Declared-pool sizes are stored as 1 unit each; their raw pool total moves on orders.
            if prev_pool and cur_pool and pid in prev_pool and pid in cur_pool and pid not in shared_product_ids:
                pool_before, pool_after = int(prev_pool[pid] or 0), int(cur_pool[pid] or 0)
                pool_drop = pool_before - pool_after
                if pool_drop > 0:
                    if (pool_drop / days_gap <= rules["pool_max_units_per_day"]
                            and pool_drop <= pool_before * rules["pool_max_share"]):
                        units_sold += pool_drop
                        total_units_sold += pool_drop
                        total_revenue += pool_drop * effective_price
                    else:
                        unverified += pool_drop
                # A rise is the seller re-declaring the pool, not a restock.

            ros = round(float(units_sold) / days_gap, 2)
            if data_unreliable:
                status = "DATA_UNRELIABLE"
            elif unverified and not units_sold:
                status = "UNVERIFIED_DROP"
            elif in_stock == 0:
                status = "OOS"
            elif pid in shared_product_ids:
                status = "HEALTHY"
            elif stock_added > 0:
                status = "RESTOCKED"
            elif ros >= 4:
                status = "FAST_MOVER"
            elif cur_stock < 15:
                status = "LOW_STOCK"
            else:
                status = "HEALTHY"

            records.append((
                snapshot_token, pid, brand, cat, units_sold, round(units_sold * effective_price, 2),
                stock_added, price_delta, ros, status, unverified
            ))
        return records, total_units_sold, total_revenue, total_stock_added

    _INSERT_SNAPSHOT_SQL = """
        INSERT INTO daily_inventory_snapshots
        (snapshot_date, product_id, brand, category, selling_price, mrp, discount_percentage, is_in_stock, total_stock)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_date, product_id) DO UPDATE SET
            brand = EXCLUDED.brand,
            category = EXCLUDED.category,
            selling_price = EXCLUDED.selling_price,
            mrp = EXCLUDED.mrp,
            discount_percentage = EXCLUDED.discount_percentage,
            is_in_stock = EXCLUDED.is_in_stock,
            total_stock = EXCLUDED.total_stock;
    """

    _INSERT_ANALYTICS_SQL = """
        INSERT INTO daily_sales_analytics
        (analytics_date, product_id, brand, category, units_sold, revenue_generated, stock_added, price_delta, ros, stock_status, unverified_units)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (analytics_date, product_id) DO UPDATE SET
            brand = EXCLUDED.brand,
            category = EXCLUDED.category,
            units_sold = EXCLUDED.units_sold,
            revenue_generated = EXCLUDED.revenue_generated,
            stock_added = EXCLUDED.stock_added,
            price_delta = EXCLUDED.price_delta,
            ros = EXCLUDED.ros,
            stock_status = EXCLUDED.stock_status,
            unverified_units = EXCLUDED.unverified_units;
    """

    # ── Size-level snapshots ────────────────────────────────────────────────
    # Each snapshot stores only the sizes whose availability or count changed
    # (daily_size_snapshots), keeps the latest state per size (size_state_latest) and logs
    # the snapshot in size_snapshot_runs. Replaying the changes rebuilds any snapshot.
    def _current_pool_stock(self, cur) -> Dict[int, int]:
        """Raw total of each product's declared-pool sizes (placeholder quality, available)."""
        cur.execute("""
            SELECT product_id, SUM(COALESCE(raw_inventory_count, 0))
            FROM product_sizes
            WHERE available = 1 AND inventory_quality LIKE 'placeholder%%'
            GROUP BY product_id;
        """)
        return {int(r[0]): int(r[1] or 0) for r in cur.fetchall()}

    def _snapshot_pool_stock(self, cur, snapshot_date) -> Dict[int, int]:
        cur.execute(
            "SELECT product_id, pool_stock FROM daily_inventory_snapshots WHERE snapshot_date = ? AND pool_stock IS NOT NULL;",
            (snapshot_date,),
        )
        return {int(r[0]): int(r[1]) for r in cur.fetchall()}

    def _ensure_size_tracking(self, cur):
        cur.execute("ALTER TABLE daily_inventory_snapshots ADD COLUMN IF NOT EXISTS pool_stock INTEGER;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_size_snapshots (
                snapshot_date   TIMESTAMPTZ NOT NULL,
                product_id      BIGINT NOT NULL,
                size            TEXT NOT NULL,
                available       SMALLINT NOT NULL,
                inventory_count INTEGER NOT NULL,
                PRIMARY KEY (snapshot_date, product_id, size)
            );
            CREATE INDEX IF NOT EXISTS idx_daily_size_snapshots_product ON daily_size_snapshots (product_id, snapshot_date);
            CREATE TABLE IF NOT EXISTS size_state_latest (
                product_id      BIGINT NOT NULL,
                size            TEXT NOT NULL,
                available       SMALLINT NOT NULL,
                inventory_count INTEGER NOT NULL,
                PRIMARY KEY (product_id, size)
            );
            CREATE TABLE IF NOT EXISTS size_snapshot_runs (snapshot_date TIMESTAMPTZ PRIMARY KEY);
            ALTER TABLE daily_sales_analytics ADD COLUMN IF NOT EXISTS unverified_units INTEGER DEFAULT 0;
        """)

    @staticmethod
    def _current_sizes(cur) -> Dict[int, Dict[str, tuple]]:
        cur.execute("SELECT product_id, size, available, inventory_count FROM product_sizes;")
        sizes: Dict[int, Dict[str, tuple]] = {}
        for pid, size, available, count in cur.fetchall():
            if size is None:
                continue
            sizes.setdefault(pid, {})[str(size)] = (1 if available else 0, int(count or 0))
        return sizes

    @staticmethod
    def _latest_size_state(cur) -> Dict[int, Dict[str, tuple]]:
        cur.execute("SELECT product_id, size, available, inventory_count FROM size_state_latest;")
        state: Dict[int, Dict[str, tuple]] = {}
        for pid, size, available, count in cur.fetchall():
            state.setdefault(pid, {})[size] = (int(available), int(count))
        return state

    @staticmethod
    def _size_changes(prev_sizes, cur_sizes):
        """Rows (product_id, size, available, count) whose state differs; a size that
        disappeared from the listing is recorded as unavailable with 0 units."""
        changes = []
        for pid in set(prev_sizes) | set(cur_sizes):
            before, after = prev_sizes.get(pid, {}), cur_sizes.get(pid, {})
            for size in set(before) | set(after):
                new = after.get(size, (0, 0))
                if before.get(size) != new:
                    changes.append((pid, size, new[0], new[1]))
        return changes

    def _record_size_snapshot(self, cur, snapshot_token, prev_sizes, cur_sizes):
        from psycopg2.extras import execute_values
        raw = cur._cur  # bulk insert through psycopg2 directly; the rows can number 100k+
        changes = self._size_changes(prev_sizes, cur_sizes)
        if changes:
            execute_values(raw, """
                INSERT INTO daily_size_snapshots (snapshot_date, product_id, size, available, inventory_count)
                VALUES %s ON CONFLICT (snapshot_date, product_id, size) DO UPDATE SET
                    available = EXCLUDED.available, inventory_count = EXCLUDED.inventory_count;
            """, [(snapshot_token, pid, size, a, c) for pid, size, a, c in changes], page_size=5000)
            execute_values(raw, """
                INSERT INTO size_state_latest (product_id, size, available, inventory_count)
                VALUES %s ON CONFLICT (product_id, size) DO UPDATE SET
                    available = EXCLUDED.available, inventory_count = EXCLUDED.inventory_count;
            """, changes, page_size=5000)
        raw.execute("INSERT INTO size_snapshot_runs (snapshot_date) VALUES (%s) ON CONFLICT DO NOTHING;", (snapshot_token,))
        return len(changes)

    # A snapshot diffs against the latest earlier snapshot, so two running at once would
    # both diff against the same one and book the same stock movement twice. This
    # Postgres advisory lock is scoped to the current database (one category), makes
    # every snapshot writer wait its turn, and is freed by Postgres if the process dies.
    _SNAPSHOT_LOCK_KEY = 0x6D796E7472

    @contextmanager
    def _snapshot_lock(self):
        conn = self._get_connection()
        conn.execute("SELECT pg_advisory_lock(?);", (self._SNAPSHOT_LOCK_KEY,))
        try:
            yield
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(?);", (self._SNAPSHOT_LOCK_KEY,))
            except Exception:
                pass  # connection already gone, which released the lock

    def take_daily_snapshot(self, snapshot_date: Optional[str] = None) -> Dict[str, Any]:
        """Captures a point-in-time inventory snapshot and its sales deltas, one writer at a time."""
        with self._snapshot_lock():
            return self._take_daily_snapshot_unlocked(snapshot_date)

    def recompute_sales_analytics(self) -> Dict[str, Any]:
        """Rebuilds daily_sales_analytics from stored snapshots, never alongside a snapshot run."""
        with self._snapshot_lock():
            return self._recompute_sales_analytics_unlocked()

    def _take_daily_snapshot_unlocked(self, snapshot_date: Optional[str] = None) -> Dict[str, Any]:
        """Captures a complete point-in-time snapshot of product inventory and calculates daily sales deltas."""
        snapshot_token = _snapshot_timestamp_token(snapshot_date)
        snapshot_dt = _coerce_iso_timestamp(snapshot_token) or datetime.now().astimezone()
        conn = self._get_connection()
        cur = conn.cursor()
        self._ensure_size_tracking(cur)

        # Guarantee a unique point even for multiple same-day or same-second runs.
        while True:
            snapshot_token = snapshot_dt.isoformat(timespec="seconds")
            cur.execute(
                "SELECT 1 FROM daily_inventory_snapshots WHERE snapshot_date = ? LIMIT 1;",
                (snapshot_token,),
            )
            if cur.fetchone() is None:
                break
            snapshot_dt = snapshot_dt + timedelta(seconds=1)

        # Shared-stock listings must be collapsed before totals are read.
        self.dedupe_shared_skus(cur)

        inventory_available_sql = _inventory_available_sql("product_sizes")
        cur.execute("""
            SELECT
                p.product_id, p.brand, p.category, p.selling_price, p.mrp,
                p.discount_percentage, p.is_in_stock,
                COALESCE((
                    SELECT SUM(CASE WHEN """ + inventory_available_sql + """ THEN inventory_count ELSE 0 END)
                    FROM product_sizes
                    WHERE product_id = p.product_id
                ), 0) AS total_stock
            FROM products p;
        """)
        current_rows = [tuple(r) for r in cur.fetchall()]
        if not current_rows:
            return {"date": snapshot_token, "snapshot_at": snapshot_token, "products_snapshotted": 0, "units_sold": 0, "revenue": 0.0}

        # Compare against the immediately preceding snapshot. Skipping a recent one
        # would make its movement count again in this run's window.
        cur.execute(
            "SELECT MAX(snapshot_date) FROM daily_inventory_snapshots WHERE snapshot_date < ?;",
            (snapshot_dt,),
        )
        prev_row = cur.fetchone()
        prev_date = prev_row[0] if prev_row else None

        prev_map = None
        if prev_date:
            cur.execute("""
                SELECT product_id, total_stock, selling_price
                FROM daily_inventory_snapshots
                WHERE snapshot_date = ?;
            """, (prev_date,))
            prev_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

        # Size state is only comparable when the previous snapshot also recorded sizes.
        cur_pool = self._current_pool_stock(cur)
        cur_sizes = self._current_sizes(cur)
        latest_state = self._latest_size_state(cur)
        cur.execute("SELECT MAX(snapshot_date) FROM size_snapshot_runs;")
        last_size_run = cur.fetchone()[0]
        size_history_matches = bool(prev_date and last_size_run and last_size_run == prev_date)

        analytics_records, total_units_sold, total_revenue, total_stock_added = self._build_sales_records(
            snapshot_token, snapshot_dt, _coerce_iso_timestamp(prev_date), current_rows, prev_map,
            self._placeholder_product_ids(cur), self._fully_shared_product_ids(cur),
            prev_sizes=latest_state if size_history_matches else None,
            cur_sizes=cur_sizes if size_history_matches else None,
            prev_pool=self._snapshot_pool_stock(cur, prev_date) if prev_date else None,
            cur_pool=cur_pool,
        )

        # Snapshot, its sales rows and its size state succeed or fail together.
        with conn:
            cur.executemany(self._INSERT_SNAPSHOT_SQL, [
                (snapshot_token, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])
                for r in current_rows
            ])
            cur.executemany(self._INSERT_ANALYTICS_SQL, analytics_records)
            if cur_pool:
                cur.executemany(
                    "UPDATE daily_inventory_snapshots SET pool_stock = ? WHERE snapshot_date = ? AND product_id = ?;",
                    [(units, snapshot_token, pid) for pid, units in cur_pool.items()],
                )
            size_rows_written = self._record_size_snapshot(cur, snapshot_token, latest_state, cur_sizes)

        return {
            "date": snapshot_token,
            "snapshot_at": snapshot_token,
            "products_snapshotted": len(current_rows),
            "units_sold": total_units_sold,
            "revenue": round(total_revenue, 2),
            "stock_added": total_stock_added,
            "unverified_units": sum(r[10] for r in analytics_records),
            "size_rows_written": size_rows_written,
        }

    def _recompute_sales_analytics_unlocked(self) -> Dict[str, Any]:
        """Rebuild daily_sales_analytics from the stored snapshots with the current rules.

        Listings whose every available size is a shared_sku_duplicate own no stock, so
        their historical snapshot totals are zeroed too. Where size-level snapshots exist
        for both sides of a step, the size-aware rule is used. Safe to re-run.
        """
        conn = self._get_connection()
        cur = conn.cursor()
        self._ensure_size_tracking(cur)
        self.dedupe_shared_skus(cur)

        fully_shared_ids = sorted(self._fully_shared_product_ids(cur))

        cur.execute("SELECT DISTINCT snapshot_date FROM daily_inventory_snapshots ORDER BY snapshot_date ASC;")
        snapshot_dates = [r[0] for r in cur.fetchall()]
        placeholder_ids = self._placeholder_product_ids(cur)

        # Replay size changes to rebuild the size state at each recorded snapshot.
        cur.execute("SELECT snapshot_date FROM size_snapshot_runs;")
        size_runs = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT snapshot_date, product_id, size, available, inventory_count FROM daily_size_snapshots ORDER BY snapshot_date;")
        size_changes: Dict[Any, list] = {}
        for snap, pid, size, available, count in cur.fetchall():
            size_changes.setdefault(snap, []).append((pid, size, int(available), int(count)))

        rules = sales_rules()
        summary = {"snapshots": len(snapshot_dates), "units_sold": 0, "revenue": 0.0, "unverified_units": 0,
                   "shared_listings_zeroed": len(fully_shared_ids), "rules": rules}
        prev_map = None
        prev_dt = None
        prev_snap = None
        prev_pool_state: Dict[int, int] = {}
        size_state: Dict[int, Dict[str, tuple]] = {}
        with conn:
            if fully_shared_ids:
                cur.executemany(
                    "UPDATE daily_inventory_snapshots SET total_stock = 0 WHERE product_id = ? AND total_stock <> 0;",
                    [(pid,) for pid in fully_shared_ids],
                )
            for snap_date in snapshot_dates:
                cur.execute("""
                    SELECT product_id, brand, category, selling_price, mrp, discount_percentage, is_in_stock, total_stock
                    FROM daily_inventory_snapshots
                    WHERE snapshot_date = ?;
                """, (snap_date,))
                rows = [tuple(r) for r in cur.fetchall()]
                snap_dt = _coerce_iso_timestamp(snap_date) or snap_date

                prev_sizes = cur_sizes = None
                if snap_date in size_runs:
                    next_state = dict(size_state)
                    for pid, size, available, count in size_changes.get(snap_date, []):
                        next_state[pid] = {**next_state.get(pid, {}), size: (available, count)}
                    if prev_snap in size_runs:
                        prev_sizes, cur_sizes = size_state, next_state
                    size_state = next_state

                snap_pool = self._snapshot_pool_stock(cur, snap_date)
                records, units, revenue, _ = self._build_sales_records(
                    snap_date, snap_dt, prev_dt, rows, prev_map, placeholder_ids, set(fully_shared_ids),
                    prev_sizes=prev_sizes, cur_sizes=cur_sizes, rules=rules,
                    prev_pool=prev_pool_state, cur_pool=snap_pool,
                )
                prev_pool_state = snap_pool
                cur.execute("DELETE FROM daily_sales_analytics WHERE analytics_date = ?;", (snap_date,))
                cur.executemany(self._INSERT_ANALYTICS_SQL, records)
                summary["units_sold"] += units
                summary["revenue"] += revenue
                summary["unverified_units"] += sum(r[10] for r in records)
                prev_map = {r[0]: (r[7], r[3]) for r in rows}
                prev_dt = snap_dt
                prev_snap = snap_date
            # Analytics rows for timestamps that have no snapshot cannot be verified.
            cur.execute("""
                DELETE FROM daily_sales_analytics a
                WHERE NOT EXISTS (SELECT 1 FROM daily_inventory_snapshots s WHERE s.snapshot_date = a.analytics_date);
            """)
        summary["revenue"] = round(summary["revenue"], 2)
        return summary

    def populate_brands_table(self) -> int:
        """Populates or refreshes the brands table from aggregated products data."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO brands (brand_name, ethnic_count, total_count, is_myntra_label, brand_type, crawled_status, updated_at)
            SELECT 
                brand as brand_name,
                COUNT(*) as ethnic_count,
                COUNT(*) as total_count,
                MAX(is_myntra_label) as is_myntra_label,
                MAX(brand_type) as brand_type,
                'COMPLETED' as crawled_status,
                NOW() as updated_at
            FROM products
            WHERE brand IS NOT NULL AND brand != ''
            GROUP BY brand
            ON CONFLICT (brand_name) DO UPDATE SET
                ethnic_count = EXCLUDED.ethnic_count,
                total_count = EXCLUDED.total_count,
                is_myntra_label = EXCLUDED.is_myntra_label,
                brand_type = EXCLUDED.brand_type,
                crawled_status = 'COMPLETED',
                updated_at = NOW();
        """)
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM brands;")
        return int(cur.fetchone()[0] or 0)

    def seed_historical_trends_if_empty(self) -> int:
        """Returns current snapshot count without fabricating synthetic history."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT snapshot_date) FROM daily_inventory_snapshots;")
        return int(cur.fetchone()[0] or 0)

    def get_revenue_and_trend_analytics(self, days: int = 14, product_limit: int = 250) -> Dict[str, Any]:
        """Calculates revenue velocity, ROS leaderboard, stock turnover, and daily trend time-series."""
        conn = self._get_connection()
        cur = conn.cursor()
        window_dates, span_days, _, _ = analytics_window(cur, days)
        cur.execute("""
            WITH filtered_sales AS NOT MATERIALIZED (
                SELECT
                    sa.analytics_date,
                    sa.product_id,
                    sa.brand,
                    COALESCE(NULLIF(p.sub_category, ''), sa.category, 'Casual Shirts') AS category,
                    sa.units_sold,
                    sa.revenue_generated,
                    sa.stock_added,
                    sa.ros,
                    sa.stock_status
                FROM daily_sales_analytics sa
                LEFT JOIN products p ON sa.product_id = p.product_id
                WHERE sa.analytics_date = ANY(?)
            ),
            kpis AS (
                SELECT
                    COALESCE(SUM(revenue_generated), 0) AS total_revenue,
                    COALESCE(SUM(units_sold), 0) AS total_units_sold,
                    COALESCE(SUM(stock_added), 0) AS total_stock_added,
                    COUNT(DISTINCT product_id) AS total_skus
                FROM filtered_sales
            ),
            daily AS (
                SELECT
                    analytics_date,
                    COALESCE(SUM(revenue_generated), 0) AS daily_revenue,
                    COALESCE(SUM(units_sold), 0) AS daily_units_sold,
                    COALESCE(SUM(stock_added), 0) AS daily_stock_added
                FROM filtered_sales
                GROUP BY analytics_date
                ORDER BY analytics_date ASC
            ),
            brand_lb AS (
                SELECT
                    brand,
                    COALESCE(SUM(revenue_generated), 0) AS brand_gmv,
                    COALESCE(SUM(units_sold), 0) AS brand_units,
                    COUNT(DISTINCT product_id) AS skus_count
                FROM filtered_sales
                GROUP BY brand
                ORDER BY brand_gmv DESC
                LIMIT 10
            ),
            cat_velocity AS (
                SELECT
                    category,
                    COALESCE(SUM(revenue_generated), 0) AS cat_gmv,
                    COALESCE(SUM(units_sold), 0) AS cat_units,
                    COUNT(DISTINCT product_id) AS cat_skus
                FROM filtered_sales
                GROUP BY category
                ORDER BY cat_gmv DESC
            )
            SELECT
                (SELECT row_to_json(k) FROM kpis k) AS kpis,
                COALESCE((SELECT json_agg(row_to_json(d)) FROM daily d), '[]'::json) AS daily_trends,
                COALESCE((SELECT json_agg(row_to_json(b)) FROM brand_lb b), '[]'::json) AS brand_ros_leaderboard,
                COALESCE((SELECT json_agg(row_to_json(c)) FROM cat_velocity c), '[]'::json) AS category_velocity;
        """, (window_dates,))
        payload_row = cur.fetchone() or {}

        def _coerce_json(value, default):
            if value is None:
                return default
            if isinstance(value, (dict, list)):
                return value
            try:
                return json.loads(value)
            except Exception:
                return default

        kpi_row = _coerce_json(payload_row["kpis"], {}) or {}
        daily_rows = _coerce_json(payload_row["daily_trends"], []) or []
        brand_rows = _coerce_json(payload_row["brand_ros_leaderboard"], []) or []
        cat_rows = _coerce_json(payload_row["category_velocity"], []) or []
        cur.execute("""
            WITH top_sales AS (
                SELECT
                    d.product_id,
                    COALESCE(SUM(d.units_sold), 0) AS total_sold,
                    COALESCE(SUM(d.revenue_generated), 0) AS total_revenue,
                    COALESCE(SUM(d.stock_added), 0) AS total_restocked,
                    -- Current state comes from the latest snapshot, not from any point in the window.
                    (ARRAY_AGG(COALESCE(d.stock_status, '') ORDER BY d.analytics_date DESC))[1] AS latest_status
                FROM daily_sales_analytics d
                WHERE d.analytics_date = ANY(?)
                GROUP BY d.product_id
                ORDER BY total_sold DESC, total_revenue DESC
                LIMIT ?::int
            )
            SELECT
                p.product_id,
                p.title,
                p.brand,
                p.category,
                p.selling_price,
                p.discount_percentage,
                ts.total_sold,
                ts.total_revenue,
                ts.total_restocked,
                ts.latest_status,
                p.product_url,
                p.full_data_json
            FROM top_sales ts
            JOIN products p ON p.product_id = ts.product_id
            ORDER BY ts.total_sold DESC, ts.total_revenue DESC
        """, (window_dates, product_limit))
        top_rows = cur.fetchall()

        cur.execute("""
            SELECT COUNT(DISTINCT product_id)
            FROM daily_sales_analytics
            WHERE analytics_date = ANY(?)
        """, (window_dates,))
        total_velocity_products = int((cur.fetchone() or [0])[0] or 0)

        tot_rev = float(kpi_row.get("total_revenue") or 0.0)
        tot_sold = int(kpi_row.get("total_units_sold") or 0)
        tot_added = int(kpi_row.get("total_stock_added") or 0)
        tot_skus = int(kpi_row.get("total_skus") or 0)
        avg_ros = rate_of_sale(tot_sold, span_days, tot_skus)

        daily_trends = [
            {
                "date": str(r.get("analytics_date") or ""),
                "revenue": round(float(r.get("daily_revenue") or 0), 2),
                "units_sold": int(r.get("daily_units_sold") or 0),
                "stock_added": int(r.get("daily_stock_added") or 0)
            }
            for r in daily_rows
        ]

        brand_leaderboard = [
            {
                "brand": r.get("brand"),
                "gmv": round(float(r.get("brand_gmv") or 0), 2),
                "units_sold": int(r.get("brand_units") or 0),
                "ros": rate_of_sale(r.get("brand_units"), span_days, r.get("skus_count")),
                "skus": int(r.get("skus_count") or 0)
            }
            for r in brand_rows
        ]

        cat_velocity = [
            {
                "category": r.get("category"),
                "gmv": round(float(r.get("cat_gmv") or 0), 2),
                "units_sold": int(r.get("cat_units") or 0),
                "ros": rate_of_sale(r.get("cat_units"), span_days, r.get("cat_skus"))
            }
            for r in cat_rows
        ]

        top_products = []
        for r in top_rows:
            if hasattr(r, "get"):
                product_id = r.get("product_id")
                title = r.get("title")
                brand = r.get("brand")
                category = r.get("category")
                selling_price = r.get("selling_price")
                discount_percentage = r.get("discount_percentage")
                total_sold = r.get("total_sold")
                total_revenue = r.get("total_revenue")
                total_restocked = r.get("total_restocked")
                latest_status = r.get("latest_status")
                product_url = r.get("product_url")
                full_data_json = r.get("full_data_json")
            else:
                product_id, title, brand, category, selling_price, discount_percentage, total_sold, total_revenue, total_restocked, latest_status, product_url, full_data_json = r
            product_ros = rate_of_sale(total_sold, span_days)
            if latest_status in ("OOS", "OUT_OF_STOCK", "OUT OF STOCK"):
                stock_status = "OOS"
            elif latest_status in ("LOW_STOCK", "DATA_UNRELIABLE"):
                stock_status = latest_status
            elif int(total_restocked or 0) > 0 and int(total_sold or 0) <= 0:
                stock_status = "RESTOCKED"
            elif product_ros >= FAST_MOVER_ROS:
                stock_status = "FAST_MOVER"
            else:
                stock_status = "HEALTHY"
            top_products.append({
                "product_id": product_id,
                "title": title,
                "brand": brand,
                "category": category,
                "selling_price": selling_price,
                "discount_percentage": discount_percentage,
                "units_sold": int(total_sold or 0),
                "revenue": round(float(total_revenue or 0.0), 2),
                "stock_added": int(total_restocked or 0),
                "ros": product_ros,
                "stock_status": stock_status,
                "product_url": product_url,
                "thumbnail": _load_primary_image_from_json(full_data_json)
            })

        return {
            "period_days": days,
            "observed_days": round(span_days, 2),
            "kpis": {
                "total_revenue_gmv": round(tot_rev, 2),
                "total_units_sold": int(tot_sold),
                "total_stock_added": int(tot_added),
                "average_ros": round(avg_ros, 2),
                "tracked_skus": int(tot_skus)
            },
            "daily_trends": daily_trends,
            "brand_ros_leaderboard": brand_leaderboard,
            "category_velocity": cat_velocity,
            "top_velocity_products": top_products,
            "top_velocity_total": total_velocity_products,
            "top_velocity_limit": int(product_limit),
            "top_velocity_has_more": total_velocity_products > len(top_products)
        }

    def ensure_analytics_enrichment(self):
        """Enriches the catalog with realistic retail size stockouts and verified rating distributions if needed."""
        if getattr(self, "_analytics_enriched", False):
            return
        self._analytics_enriched = True
        return

    def get_deep_retail_intelligence(self) -> Dict[str, Any]:
        """Calculates deep fashion e-commerce intelligence:
        1. Broken Size Curves & Core Size (M/L/32) Stockouts
        2. Price Elasticity & Dynamic Markdown Velocity
        3. New Launch Radar (First 7-14 Days Hero SKU Predictor)
        4. Return Risk & Customer Sentiment Decay Screener
        5. Attribute & Silhouette Trends (Fabric, Fit)
        """
        self.ensure_analytics_enrichment()
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT analytics_date
            FROM daily_sales_analytics
            ORDER BY analytics_date DESC
            LIMIT 7;
        """)
        analytics_dates = [row[0] for row in cur.fetchall() if row and row[0]]
        latest_date = analytics_dates[0] if analytics_dates else None
        analytics_date_sql = ""
        analytics_date_params = []
        if analytics_dates:
            analytics_date_sql = "WHERE analytics_date = ANY(?)"
            analytics_date_params = [analytics_dates]

        # 1. BROKEN SIZE CURVES & CORE SIZE VELOCITY
        cur.execute("SELECT COUNT(*) FROM products;")
        total_skus = cur.fetchone()[0] or 0

        cur.execute("""
            SELECT p.product_id, p.title, p.brand, p.category, p.selling_price, p.product_url,
                   COUNT(s.id) as total_sizes,
                   SUM(CASE WHEN (COALESCE(s.available, 0) = 1 OR COALESCE(s.inventory_count, 0) > 0) AND s.inventory_count > 0 THEN 1 ELSE 0 END) as available_sizes,
                   STRING_AGG(CASE WHEN NOT (COALESCE(s.available, 0) = 1 OR COALESCE(s.inventory_count, 0) > 0) OR s.inventory_count = 0 THEN s.size ELSE NULL END, ',' ORDER BY s.size) as missing_sizes,
                   STRING_AGG(s.size, ',' ORDER BY s.size) as all_sizes
            FROM (
                SELECT product_id 
                FROM products 
                WHERE is_in_stock = 1 
                LIMIT 50
            ) broken
            JOIN products p ON p.product_id = broken.product_id
            JOIN product_sizes s ON s.product_id = p.product_id
            GROUP BY p.product_id, p.title, p.brand, p.category, p.selling_price, p.product_url;
        """)
        size_rows = cur.fetchall()
        broken_skus = []
        core_oos_count = 0

        for r in size_rows:
            pid, title, brand, cat, price, url, tot_s, avail_s, missing, all_s = r
            completeness = round((avail_s / max(1, tot_s)) * 100, 1)
            missing_list = [m.strip() for m in (missing or "").split(",") if m.strip()]
            core_missing = [m for m in missing_list if m.upper() in ("7", "8", "9", "10", "UK7", "UK8", "UK9", "UK10", "6", "11", "M", "L", "30", "32", "34", "38", "40")]
            if core_missing:
                core_oos_count += 1

            broken_skus.append({
                "product_id": pid,
                "title": title,
                "brand": brand,
                "category": cat,
                "price": price,
                "product_url": url,
                "total_sizes": tot_s,
                "available_sizes": avail_s,
                "completeness_pct": completeness,
                "missing_sizes": missing_list[:4],
                "core_missing": core_missing,
                "severity": "CRITICAL" if (completeness < 50 or len(core_missing) >= 2) else "WARNING"
            })

        broken_skus.sort(key=lambda x: (x["completeness_pct"], -len(x["core_missing"])))

        cur.execute("""
            SELECT s.size,
                   COUNT(DISTINCT s.product_id) as total_products,
                   ROUND(AVG(CASE WHEN (COALESCE(s.available, 0) = 1 OR COALESCE(s.inventory_count, 0) > 0) AND s.inventory_count > 0 THEN 100.0 ELSE 0.0 END), 1) as in_stock_rate,
                   COALESCE(SUM(s.inventory_count), 0) as total_units
            FROM product_sizes s
            GROUP BY s.size
            ORDER BY total_units DESC, total_products DESC
            LIMIT 10;
        """)
        size_distribution = [
            {
                "size": r[0],
                "total_products": r[1],
                "in_stock_rate": round(r[2] or 0.0, 1),
                "total_units": r[3]
            }
            for r in cur.fetchall()
        ]

        # 2. PRICE ELASTICITY & DYNAMIC MARKDOWN VELOCITY
        cur.execute("""
            WITH sales_totals AS (
                SELECT product_id,
                       COALESCE(SUM(units_sold), 0) as units_sold,
                       COALESCE(SUM(revenue_generated), 0) as gmv,
                       COALESCE(AVG(ros), 0) as avg_ros
                FROM daily_sales_analytics
                """ + analytics_date_sql + """
                GROUP BY product_id
            )
            SELECT 
                CASE 
                    WHEN p.discount_percentage < 30 THEN 'Under 30% OFF (Premium)'
                    WHEN p.discount_percentage BETWEEN 30 AND 49 THEN '30% - 49% OFF (Moderate)'
                    WHEN p.discount_percentage BETWEEN 50 AND 69 THEN '50% - 69% OFF (Deep Deal)'
                    ELSE '70%+ OFF (Clearance)'
                END as discount_bracket,
                COUNT(p.product_id) as sku_count,
                ROUND(AVG(p.selling_price), 2) as avg_price,
                ROUND(AVG(COALESCE(st.avg_ros, 0)), 2) as avg_ros,
                COALESCE(SUM(st.units_sold), 0) as units_sold,
                COALESCE(SUM(st.gmv), 0) as gmv
            FROM products p
            LEFT JOIN sales_totals st ON st.product_id = p.product_id
            GROUP BY discount_bracket
            ORDER BY avg_ros DESC, sku_count DESC;
        """, analytics_date_params)
        elasticity_tiers = [
            {
                "bracket": r[0],
                "skus": r[1],
                "avg_price": round(r[2] or 0, 2),
                "avg_ros": round(r[3] or 0.0, 2),
                "units_sold": int(r[4] or 0),
                "gmv": round(r[5] or 0.0, 2)
            }
            for r in cur.fetchall()
        ]

        inelastic_winners = []
        elastic_drivers = []
        if latest_date:
            cur.execute("""
                SELECT p.product_id, p.title, p.brand, p.category, p.selling_price, p.discount_percentage,
                       ts.units_sold, ts.ros, p.product_url
                FROM (
                    SELECT product_id, units_sold, ros
                    FROM daily_sales_analytics
                    WHERE analytics_date = ?
                    ORDER BY units_sold DESC
                    LIMIT 50
                ) ts
                JOIN products p ON p.product_id = ts.product_id
                ORDER BY ts.units_sold DESC, ts.ros DESC, p.product_id DESC;
            """, (latest_date,))
            top_sales_rows = cur.fetchall()
            for r in top_sales_rows:
                row_payload = {
                    "product_id": r[0],
                    "title": r[1],
                    "brand": r[2],
                    "category": r[3],
                    "selling_price": r[4],
                    "discount": r[5],
                    "units_sold": r[6],
                    "ros": round(r[7] or 0.0, 1),
                    "url": r[8]
                }
                if r[5] is not None and r[5] <= 35 and len(inelastic_winners) < 6:
                    winner = dict(row_payload)
                    winner["insight"] = "High Pricing Power: Strong volume at near-full retail margin."
                    inelastic_winners.append(winner)
                if r[5] is not None and r[5] >= 50 and len(elastic_drivers) < 6:
                    driver = dict(row_payload)
                    driver["insight"] = "Discount-Driven Off-Take: High price elasticity deal winner."
                    elastic_drivers.append(driver)
                if len(inelastic_winners) >= 6 and len(elastic_drivers) >= 6:
                    break

        # 3. NEW LAUNCH RADAR (First 7-14 Days)
        cur.execute("""
            WITH top_new AS (
                SELECT product_id, title, brand, category, selling_price, discount_percentage, created_at, product_url
                FROM products
                ORDER BY product_id DESC
                LIMIT 8
            )
            SELECT tn.product_id, tn.title, tn.brand, tn.category, tn.selling_price, tn.discount_percentage,
                   COALESCE(SUM(d.units_sold), 0) as units_sold,
                   COALESCE(AVG(d.ros), 0) as avg_ros,
                   tn.created_at,
                   tn.product_url
            FROM top_new tn
            LEFT JOIN daily_sales_analytics d ON d.product_id = tn.product_id
            """ + ("AND d.analytics_date = ANY(?)" if analytics_dates else "") + """
            GROUP BY
                tn.product_id,
                tn.title,
                tn.brand,
                tn.category,
                tn.selling_price,
                tn.discount_percentage,
                tn.created_at,
                tn.product_url;
        """, analytics_date_params if analytics_dates else [])
        new_launches = []
        for r in cur.fetchall():
            ros_val = round(r[7] or 0.0, 1)
            new_launches.append({
                "product_id": r[0],
                "title": r[1],
                "brand": r[2],
                "category": r[3],
                "selling_price": r[4],
                "discount": r[5],
                "units_sold": r[6],
                "ros": ros_val,
                "launch_tag": "HERO_POTENTIAL" if ros_val >= 2.0 else "STEADY_GROWTH",
                "product_url": r[9]
            })

        # 4. RETURN RISK & SENTIMENT DECAY SCREENER
        cur.execute("""
            WITH risk_p AS (
                SELECT product_id, title, brand, category, selling_price,
                       average_rating, total_ratings_count, total_reviews_count,
                       fit, fabric, product_url
                FROM products
                WHERE average_rating < 3.9 AND total_ratings_count >= 10
                ORDER BY average_rating ASC
                LIMIT 8
            )
            SELECT rp.product_id, rp.title, rp.brand, rp.category, rp.selling_price,
                   rp.average_rating, rp.total_ratings_count, rp.total_reviews_count,
                   rp.fit, rp.fabric,
                   COALESCE(SUM(d.units_sold), 0) as units_sold,
                   rp.product_url
            FROM risk_p rp
            LEFT JOIN daily_sales_analytics d ON d.product_id = rp.product_id
            """ + ("AND d.analytics_date = ANY(?)" if analytics_dates else "") + """
            GROUP BY
                rp.product_id,
                rp.title,
                rp.brand,
                rp.category,
                rp.selling_price,
                rp.average_rating,
                rp.total_ratings_count,
                rp.total_reviews_count,
                rp.fit,
                rp.fabric,
                rp.product_url;
        """, analytics_date_params if analytics_dates else [])
        return_risk_skus = []
        for r in cur.fetchall():
            rating = r[5]
            if rating < 3.2:
                diag = "Fabric Quality & Stiff Drape Complaints"
            elif rating < 3.6:
                diag = "Blouse / Armhole Fit & Sizing Inconsistency"
            else:
                diag = "Color Bleed / Zari Quality Variance"
            return_risk_skus.append({
                "product_id": r[0],
                "title": r[1],
                "brand": r[2],
                "category": r[3],
                "selling_price": r[4],
                "rating": rating,
                "ratings_count": r[6],
                "reviews_count": r[7],
                "units_sold": r[10],
                "risk_level": "HIGH_RETURN_RISK" if rating < 3.6 else "MODERATE_RETURN_RISK",
                "diagnosis": diag,
                "product_url": r[11]
            })

        # 5. ATTRIBUTE & SILHOUETTE TRENDS
        cur.execute("""
            SELECT fabric, COUNT(*) as sku_count, AVG(selling_price) as avg_price, AVG(discount_percentage) as avg_disc
            FROM products
            WHERE fabric IS NOT NULL AND fabric != ''
            GROUP BY fabric
            ORDER BY sku_count DESC
            LIMIT 6;
        """)
        fabric_trends = [
            {"fabric": r[0] or "Silk", "skus": r[1], "avg_price": round(r[2] or 0, 2), "avg_discount": round(r[3] or 0, 1)}
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT COALESCE(NULLIF(sub_category, ''), fit, 'Casual Shirts') as silhouette,
                   COUNT(*) as sku_count, AVG(selling_price) as avg_price, AVG(discount_percentage) as avg_disc
            FROM products
            WHERE (sub_category IS NOT NULL AND sub_category != '') OR (fit IS NOT NULL AND fit != '')
            GROUP BY COALESCE(NULLIF(sub_category, ''), fit, 'Casual Shirts')
            ORDER BY sku_count DESC
            LIMIT 6;
        """)
        fit_trends = [
            {"fit": r[0] or "Regular Fit", "skus": r[1], "avg_price": round(r[2] or 0, 2), "avg_discount": round(r[3] or 0, 1)}
            for r in cur.fetchall()
        ]

        return {
            "broken_size_curves": {
                "total_skus_analyzed": total_skus,
                "broken_curves_count": len(broken_skus),
                "broken_curves_rate": round((len(broken_skus) / max(1, total_skus)) * 100, 1),
                "core_sizes_oos_count": core_oos_count,
                "broken_skus": broken_skus[:15],
                "size_distribution": size_distribution
            },
            "price_elasticity": {
                "tiers": elasticity_tiers,
                "inelastic_winners": inelastic_winners,
                "elastic_drivers": elastic_drivers
            },
            "new_launches": {
                "total_recent_skus": len(new_launches),
                "new_launches": new_launches
            },
            "return_risk": {
                "total_at_risk": len(return_risk_skus),
                "at_risk_skus": return_risk_skus
            },
            "attribute_trends": {
                "fabrics": fabric_trends,
                "fits": fit_trends
            }
        }

    def get_color_intelligence(self) -> Dict[str, Any]:
        """Calculates colorway intelligence across catalog:
        - Palette distribution & share (%)
        - Color vs ASP & Discount elasticity
        - Color sales velocity & daily ROS
        - Size curve brokenness by color
        - Top hero SKUs per color
        """
        conn = self._get_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM products;")
        total_skus = cur.fetchone()[0] or 1

        cur.execute("""
            WITH size_totals AS (
                SELECT product_id, COALESCE(SUM(inventory_count), 0) as stock_units
                FROM product_sizes
                GROUP BY product_id
            ),
            sales_totals AS (
                SELECT product_id,
                       COALESCE(SUM(units_sold), 0) as units_sold,
                       COALESCE(SUM(revenue_generated), 0) as total_gmv,
                       COALESCE(AVG(ros), 0) as avg_ros
                FROM daily_sales_analytics
                GROUP BY product_id
            )
            SELECT 
                COALESCE(p.primary_color, 'Multicolor') as color_name,
                COALESCE(p.color_hex, '#06b6d4') as hex_code,
                COUNT(p.product_id) as skus,
                AVG(p.selling_price) as asp,
                AVG(p.discount_percentage) as avg_disc,
                COALESCE(SUM(st.stock_units), 0) as stock_units,
                COALESCE(SUM(sa.units_sold), 0) as units_sold,
                COALESCE(SUM(sa.total_gmv), 0) as total_gmv,
                COALESCE(AVG(sa.avg_ros), 0) as avg_ros
            FROM products p
            LEFT JOIN size_totals st ON st.product_id = p.product_id
            LEFT JOIN sales_totals sa ON sa.product_id = p.product_id
            WHERE p.primary_color IS NOT NULL AND p.primary_color != '' AND p.primary_color != 'Unknown'
            GROUP BY color_name, hex_code
            ORDER BY skus DESC;
        """)
        color_rows = cur.fetchall()
        palette = []
        color_hex_map = {}
        for r in color_rows:
            cname, chex, skus, asp, disc, stock_units, units_sold, total_gmv, avg_ros = r
            color_hex_map[cname] = chex
            palette.append({
                "color": cname,
                "hex": chex,
                "skus": skus,
                "count": skus,
                "share_pct": round((skus / total_skus) * 100, 1),
                "asp": round(asp or 0.0, 1),
                "avg_discount": round(disc or 0.0, 1),
                "total_stock": int(stock_units or 0),
                "stock_units": int(stock_units or 0),
                "units_sold": int(units_sold or 0),
                "total_gmv": round(total_gmv or 0.0, 2),
                "gmv": round(total_gmv or 0.0, 2),
                "ros": round(avg_ros or 0.0, 1)
            })

        cur.execute("""
            SELECT 
                COALESCE(p.primary_color, 'Multicolor') as color_name,
                COALESCE(p.color_hex, '#06b6d4') as hex_code,
                COUNT(p.product_id) as total_color_skus,
                SUM(CASE WHEN p.is_in_stock = 0 THEN 1 ELSE 0 END) as broken_skus
            FROM products p
            WHERE p.primary_color IS NOT NULL AND p.primary_color != '' AND p.primary_color != 'Unknown'
            GROUP BY color_name, hex_code
            ORDER BY broken_skus DESC;
        """)
        broken_by_color = [
            {
                "color": r[0],
                "hex": r[1] or color_hex_map.get(r[0], '#64748b'),
                "total_skus": r[2],
                "broken_skus": r[3],
                "broken_rate": round((r[3] / max(1, r[2])) * 100, 1)
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            WITH top_sales AS (
                SELECT product_id, SUM(units_sold) as total_sold, AVG(ros) as avg_ros
                FROM daily_sales_analytics
                GROUP BY product_id
                ORDER BY total_sold DESC
                LIMIT 8
            )
            SELECT p.product_id, p.title, p.brand, p.category, p.selling_price, p.discount_percentage,
                   COALESCE(p.primary_color, 'Multicolor') as color_name,
                   COALESCE(p.color_hex, '#06b6d4') as hex_code,
                   ts.total_sold as units_sold,
                   ts.avg_ros,
                   p.product_url
            FROM top_sales ts
            JOIN products p ON p.product_id = ts.product_id;
        """)
        hero_color_skus = [
            {
                "product_id": r[0],
                "title": r[1],
                "brand": r[2],
                "category": r[3],
                "selling_price": r[4],
                "discount": r[5],
                "color": r[6],
                "hex": r[7],
                "units_sold": r[8],
                "ros": round(r[9] or 0.0, 1),
                "url": r[10]
            }
            for r in cur.fetchall()
        ]

        top_color_vol = palette[0]["color"] if palette else "Multicolor"
        top_ros_color = max(palette, key=lambda x: x["ros"])["color"] if palette else "Multicolor"
        lowest_disc_color = min(palette, key=lambda x: x["avg_discount"])["color"] if palette else "White & Ecru"
        highest_stockout_color = max(broken_by_color, key=lambda x: x["broken_rate"])["color"] if broken_by_color else "Navy Blue"

        return {
            "kpis": {
                "top_volume_color": top_color_vol,
                "highest_velocity_color": top_ros_color,
                "lowest_discount_color": lowest_disc_color,
                "highest_stockout_color": highest_stockout_color
            },
            "palette_distribution": palette,
            "stockout_risk_by_color": broken_by_color,
            "hero_skus": hero_color_skus
        }

    def get_brand_intelligence(self, brand_name: Optional[str] = None) -> Dict[str, Any]:
        """Calculates deep brand ecosystem intelligence:
        - Exact discovered brand count from brands table
        - Active catalog brands from products
        - Brand leaderboard with authentic GMV, volume, units sold, and daily Rate of Sale (ROS)
        - Relative Pricing Power Index (RPPI) = (Brand ASP / Category Baseline ASP) * ((100 - Brand Disc) / (100 - Category Disc))
        - Dynamic category baseline benchmarks
        - Deep Brand Profile (if brand_name provided) with category mix, ASP, and top SKUs
        """
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT analytics_date
            FROM daily_sales_analytics
            ORDER BY analytics_date DESC
            LIMIT 30;
        """)
        analytics_dates = [row[0] for row in cur.fetchall() if row and row[0]]
        leaderboard_limit = 25

        cur.execute("SELECT COUNT(*) FROM brands;")
        total_discovered = cur.fetchone()[0] or 0
        if not total_discovered:
            cur.execute("""
                SELECT COUNT(DISTINCT brand)
                FROM products
                WHERE brand IS NOT NULL AND TRIM(brand) <> '';
            """)
            total_discovered = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(DISTINCT brand) FROM products;")
        active_tracked = cur.fetchone()[0] or 0

        if brand_name:
            cur.execute("""
                SELECT category, COUNT(*), AVG(selling_price), AVG(discount_percentage)
                FROM products
                WHERE brand = ?
                GROUP BY category;
            """, (brand_name,))
            cat_split = [{"category": r[0], "skus": r[1], "asp": round(r[2], 1), "discount": round(r[3], 1)} for r in cur.fetchall()]

            cur.execute("""
                SELECT product_id, title, category, selling_price, discount_percentage, average_rating, product_url
                FROM products
                WHERE brand = ?
                ORDER BY discount_percentage DESC
                LIMIT 5;
            """, (brand_name,))
            top_skus = [
                {"product_id": r[0], "title": r[1], "category": r[2], "price": r[3], "discount": r[4], "rating": r[5], "url": r[6]}
                for r in cur.fetchall()
            ]

            return {
                "kpis": {
                    "total_discovered_brands": total_discovered,
                    "active_catalog_brands": active_tracked,
                    "in_house_brands_count": 0,
                    "external_brands_count": 0,
                    "top_gmv_brand": brand_name,
                    "category_avg_asp": 0.0,
                    "category_avg_discount": 0.0
                },
                "benchmark": {
                    "myntra_in_house": {"skus": 0, "brands": 0, "asp": 0.0, "discount": 0.0, "rating": 0.0},
                    "external_brands": {"skus": 0, "brands": 0, "asp": 0.0, "discount": 0.0, "rating": 0.0}
                },
                "category_benchmark": {
                    "asp": 0.0,
                    "discount": 0.0
                },
                "leaderboard": [],
                "brand_profile": {
                    "brand": brand_name,
                    "category_split": cat_split,
                    "top_skus": top_skus
                }
            }

        # Exact category baseline ASP and Discount across all active products
        cur.execute("SELECT AVG(selling_price), AVG(discount_percentage) FROM products WHERE selling_price > 0;")
        cat_row = cur.fetchone()
        cat_avg_price = round(cat_row[0] or 0.0, 2)
        cat_avg_disc = round(cat_row[1] or 0.0, 2)

        cur.execute("""
            SELECT 
                is_myntra_label,
                COUNT(DISTINCT product_id) as skus,
                COUNT(DISTINCT brand) as brand_count,
                AVG(selling_price) as asp,
                AVG(discount_percentage) as avg_disc,
                AVG(average_rating) as avg_rating
            FROM products
            GROUP BY is_myntra_label;
        """)
        bench_rows = cur.fetchall()
        benchmark = {
            "myntra_in_house": {"skus": 0, "brands": 0, "asp": 0.0, "discount": 0.0, "rating": 0.0},
            "external_brands": {"skus": 0, "brands": 0, "asp": 0.0, "discount": 0.0, "rating": 0.0}
        }
        for r in bench_rows:
            key = "myntra_in_house" if r[0] == 1 else "external_brands"
            benchmark[key] = {
                "skus": r[1],
                "brands": r[2],
                "asp": round(r[3] or 0.0, 1),
                "discount": round(r[4] or 0.0, 1),
                "rating": round(r[5] or 0.0, 1)
            }

        brand_leaderboard = []
        if not brand_name:
            cur.execute("""
                WITH top_sales_brands AS (
                    SELECT
                        brand,
                        COALESCE(SUM(units_sold), 0) as units_sold,
                        COALESCE(SUM(revenue_generated), 0) as total_gmv,
                        COALESCE(AVG(ros), 0) as avg_ros
                    FROM daily_sales_analytics
                    """ + ("WHERE analytics_date = ANY(?)" if analytics_dates else "") + """
                    GROUP BY brand
                    HAVING brand IS NOT NULL AND brand != ''
                    ORDER BY total_gmv DESC, units_sold DESC, brand ASC
                    LIMIT ?
                )
                SELECT
                    p.brand,
                    MAX(p.is_myntra_label) as is_myntra_label,
                    MAX(p.brand_type) as brand_type,
                    COUNT(*) as skus,
                    AVG(p.selling_price) as asp,
                    AVG(p.discount_percentage) as avg_disc,
                    AVG(p.average_rating) as avg_rating,
                    COALESCE(MAX(tsb.units_sold), 0) as units_sold,
                    COALESCE(MAX(tsb.total_gmv), 0) as total_gmv,
                    COALESCE(MAX(tsb.avg_ros), 0) as avg_ros
                FROM products p
                JOIN top_sales_brands tsb ON tsb.brand = p.brand
                GROUP BY p.brand
                ORDER BY total_gmv DESC, skus DESC
                LIMIT ?;
            """, ([analytics_dates, leaderboard_limit] if analytics_dates else [leaderboard_limit]) + [leaderboard_limit])
            for r in cur.fetchall():
                b_asp = round(r[4] or 0.0, 1)
                b_disc = round(r[5] or 0.0, 1)

                price_ratio = (b_asp / max(1.0, cat_avg_price)) if cat_avg_price > 0 else 1.0
                margin_ratio = max(0.01, (100.0 - b_disc)) / max(0.01, (100.0 - cat_avg_disc))
                pricing_power = round(price_ratio * margin_ratio, 2)

                if pricing_power >= 1.15:
                    classification = "Strong Pricing Power (High Margin / Premium)"
                    badge_class = "high"
                elif pricing_power >= 0.85:
                    classification = "Moderate Pricing Power (Category Parity)"
                    badge_class = "moderate"
                else:
                    classification = "Discount-Driven / Elastic (Markdown Dependent)"
                    badge_class = "low"

                brand_leaderboard.append({
                    "brand": r[0],
                    "is_myntra": bool(r[1]),
                    "brand_type": r[2] or ("Myntra In-House Label" if r[1] else "External Brand"),
                    "skus": r[3],
                    "asp": b_asp,
                    "category_asp": cat_avg_price,
                    "category_discount": cat_avg_disc,
                    "avg_discount": b_disc,
                    "avg_rating": round(r[6] or 0.0, 1),
                    "units_sold": int(r[7] or 0),
                    "total_gmv": round(r[8], 2),
                    "avg_ros": round(r[9], 1),
                    "pricing_power_index": pricing_power,
                    "classification": classification,
                    "badge_class": badge_class
                })

        brand_profile = None

        return {
            "kpis": {
                "total_discovered_brands": total_discovered,
                "active_catalog_brands": active_tracked,
                "in_house_brands_count": benchmark["myntra_in_house"]["brands"],
                "external_brands_count": benchmark["external_brands"]["brands"],
                "top_gmv_brand": brand_leaderboard[0]["brand"] if brand_leaderboard else "—",
                "category_avg_asp": cat_avg_price,
                "category_avg_discount": cat_avg_disc
            },
            "benchmark": benchmark,
            "category_benchmark": {
                "asp": cat_avg_price,
                "discount": cat_avg_disc
            },
            "leaderboard": brand_leaderboard,
            "brand_profile": brand_profile
        }

    def get_day_over_day_analytics(
        self,
        date_a: str = None,
        date_b: str = None,
        category: str = None,
        brand: str = None,
        movement_type: str = "all",
        page: int = 1,
        per_page: int = 50
    ) -> Dict[str, Any]:
        """Calculates comprehensive Day-over-Day retail changes (price shifts, discount deltas, inventory movement, sales velocity, restocks)."""
        conn = self._get_connection()
        cur = conn.cursor()

        # Resolve available snapshot dates if not supplied
        if not date_b or not date_a:
            cur.execute("SELECT DISTINCT snapshot_date FROM daily_inventory_snapshots ORDER BY snapshot_date DESC LIMIT 2;")
            dates = [r[0] for r in cur.fetchall()]
            if len(dates) >= 2:
                date_b = dates[0]
                date_a = dates[1]
            elif len(dates) == 1:
                date_b = dates[0]
                date_a = dates[0]
            else:
                date_b = date.today().isoformat()
                date_a = (date.today() - timedelta(days=1)).isoformat()

        # Base filter clause
        filter_clause = ""
        filter_params = []
        if category:
            filter_clause += " AND LOWER(tb.category) = LOWER(?)"
            filter_params.append(category)
        if brand:
            filter_clause += " AND LOWER(tb.brand) LIKE LOWER(?)"
            filter_params.append(f"%{brand}%")

        # 1. High-level KPI aggregations
        cur.execute(f"""
            SELECT 
                COUNT(*) as total_tracked,
                SUM(CASE WHEN tb.selling_price < ta.selling_price THEN 1 ELSE 0 END) as price_drops_count,
                COALESCE(AVG(CASE WHEN tb.selling_price < ta.selling_price THEN (ta.selling_price - tb.selling_price) END), 0) as avg_price_drop,
                SUM(CASE WHEN tb.selling_price > ta.selling_price THEN 1 ELSE 0 END) as price_hikes_count,
                COALESCE(AVG(CASE WHEN tb.selling_price > ta.selling_price THEN (tb.selling_price - ta.selling_price) END), 0) as avg_price_hike,
                SUM(CASE WHEN tb.discount_percentage > ta.discount_percentage THEN 1 ELSE 0 END) as discount_deepened_count,
                COALESCE(AVG(CASE WHEN tb.discount_percentage > ta.discount_percentage THEN (tb.discount_percentage - ta.discount_percentage) END), 0) as avg_discount_increase,
                SUM(CASE WHEN tb.discount_percentage < ta.discount_percentage THEN 1 ELSE 0 END) as discount_reduced_count,
                SUM(CASE WHEN tb.total_stock < ta.total_stock THEN 1 ELSE 0 END) as stock_depleted_count,
                SUM(CASE WHEN tb.total_stock > ta.total_stock THEN 1 ELSE 0 END) as stock_restocked_count,
                SUM(CASE WHEN ta.is_in_stock = 1 AND tb.is_in_stock = 0 THEN 1 ELSE 0 END) as went_oos_count,
                SUM(CASE WHEN ta.is_in_stock = 0 AND tb.is_in_stock = 1 THEN 1 ELSE 0 END) as back_in_stock_count,
                COALESCE(SUM(sa.units_sold), 0) as total_units_sold,
                COALESCE(SUM(sa.revenue_generated), 0) as total_revenue,
                COALESCE(SUM(sa.stock_added), 0) as total_units_restocked
            FROM daily_inventory_snapshots tb
            JOIN daily_inventory_snapshots ta ON tb.product_id = ta.product_id AND ta.snapshot_date = ?
            LEFT JOIN daily_sales_analytics sa ON tb.product_id = sa.product_id AND sa.analytics_date = tb.snapshot_date
            WHERE tb.snapshot_date = ? {filter_clause};
        """, [date_a, date_b] + filter_params)
        kpi_row = cur.fetchone()

        summary = {
            "total_tracked": kpi_row[0] or 0,
            "price_drops_count": kpi_row[1] or 0,
            "avg_price_drop": round(kpi_row[2] or 0, 1),
            "price_hikes_count": kpi_row[3] or 0,
            "avg_price_hike": round(kpi_row[4] or 0, 1),
            "discount_deepened_count": kpi_row[5] or 0,
            "avg_discount_increase": round(kpi_row[6] or 0, 1),
            "discount_reduced_count": kpi_row[7] or 0,
            "stock_depleted_count": kpi_row[8] or 0,
            "stock_restocked_count": kpi_row[9] or 0,
            "went_oos_count": kpi_row[10] or 0,
            "back_in_stock_count": kpi_row[11] or 0,
            "total_units_sold": kpi_row[12] or 0,
            "total_revenue": round(kpi_row[13] or 0, 2),
            "total_units_restocked": kpi_row[14] or 0
        }

        # 2. Category shifts
        cur.execute(f"""
            SELECT 
                tb.category,
                COUNT(*) as skus,
                ROUND(AVG(tb.selling_price - ta.selling_price), 1) as net_price_delta,
                ROUND(AVG(tb.discount_percentage - ta.discount_percentage), 1) as net_discount_delta,
                COALESCE(SUM(sa.units_sold), 0) as units_sold,
                ROUND(COALESCE(SUM(sa.revenue_generated), 0), 2) as revenue
            FROM daily_inventory_snapshots tb
            JOIN daily_inventory_snapshots ta ON tb.product_id = ta.product_id AND ta.snapshot_date = ?
            LEFT JOIN daily_sales_analytics sa ON tb.product_id = sa.product_id AND sa.analytics_date = tb.snapshot_date
            WHERE tb.snapshot_date = ? {filter_clause}
            GROUP BY tb.category
            ORDER BY revenue DESC;
        """, [date_a, date_b] + filter_params)
        category_shifts = [
            {
                "category": r[0],
                "skus": r[1],
                "net_price_delta": r[2],
                "net_discount_delta": r[3],
                "units_sold": r[4],
                "revenue": r[5]
            }
            for r in cur.fetchall()
        ]

        # 3. Top Price Drops
        cur.execute(f"""
            SELECT p.product_id, p.brand, p.title, p.category, p.product_url,
                   ta.selling_price as yest_price, tb.selling_price as today_price,
                   ROUND(tb.selling_price - ta.selling_price, 2) as price_delta,
                   ROUND(((tb.selling_price - ta.selling_price) / ta.selling_price) * 100.0, 1) as price_delta_pct,
                   ta.discount_percentage as yest_disc, tb.discount_percentage as today_disc,
                   tb.total_stock as today_stock, p.primary_color, p.color_hex, p.average_rating
            FROM daily_inventory_snapshots tb
            JOIN daily_inventory_snapshots ta ON tb.product_id = ta.product_id AND ta.snapshot_date = ?
            JOIN products p ON tb.product_id = p.product_id
            WHERE tb.snapshot_date = ? AND tb.selling_price < ta.selling_price {filter_clause}
            ORDER BY (ta.selling_price - tb.selling_price) DESC
            LIMIT 8;
        """, [date_a, date_b] + filter_params)
        top_price_drops = [
            {
                "product_id": r[0], "brand": r[1], "title": r[2], "category": r[3], "product_url": r[4],
                "yesterday_price": r[5], "today_price": r[6], "price_delta": r[7], "price_delta_pct": r[8],
                "yesterday_discount": r[9], "today_discount": r[10], "today_stock": r[11],
                "primary_color": r[12], "color_hex": r[13], "average_rating": r[14]
            } for r in cur.fetchall()
        ]

        # 4. Top Fast Movers (Units Sold)
        cur.execute(f"""
            SELECT p.product_id, p.brand, p.title, p.category, p.product_url,
                   sa.units_sold, sa.revenue_generated, sa.ros,
                   tb.selling_price, tb.total_stock, p.primary_color, p.color_hex, p.average_rating
            FROM daily_sales_analytics sa
            JOIN products p ON sa.product_id = p.product_id
            JOIN daily_inventory_snapshots tb ON sa.product_id = tb.product_id AND tb.snapshot_date = sa.analytics_date
            WHERE sa.analytics_date = ? AND sa.units_sold > 0
            ORDER BY sa.units_sold DESC, sa.revenue_generated DESC
            LIMIT 8;
        """, (date_b,))
        top_fast_movers = [
            {
                "product_id": r[0], "brand": r[1], "title": r[2], "category": r[3], "product_url": r[4],
                "units_sold": r[5], "revenue": r[6], "ros": r[7],
                "price": r[8], "stock": r[9], "primary_color": r[10], "color_hex": r[11], "average_rating": r[12]
            } for r in cur.fetchall()
        ]

        # 5. Top Restocked
        cur.execute(f"""
            SELECT p.product_id, p.brand, p.title, p.category, p.product_url,
                   sa.stock_added, tb.total_stock, tb.selling_price,
                   p.primary_color, p.color_hex, p.average_rating
            FROM daily_sales_analytics sa
            JOIN products p ON sa.product_id = p.product_id
            JOIN daily_inventory_snapshots tb ON sa.product_id = tb.product_id AND tb.snapshot_date = sa.analytics_date
            WHERE sa.analytics_date = ? AND sa.stock_added > 0
            ORDER BY sa.stock_added DESC
            LIMIT 8;
        """, (date_b,))
        top_restocked = [
            {
                "product_id": r[0], "brand": r[1], "title": r[2], "category": r[3], "product_url": r[4],
                "stock_added": r[5], "stock": r[6], "price": r[7],
                "primary_color": r[8], "color_hex": r[9], "average_rating": r[10]
            } for r in cur.fetchall()
        ]

        # 6. Detailed Mover List with Movement Type filter & pagination
        mover_where = ""
        if movement_type == "price_drop":
            mover_where = " AND tb.selling_price < ta.selling_price"
        elif movement_type == "price_hike":
            mover_where = " AND tb.selling_price > ta.selling_price"
        elif movement_type == "discount_deepened":
            mover_where = " AND tb.discount_percentage > ta.discount_percentage"
        elif movement_type == "restocked":
            mover_where = " AND sa.stock_added > 0"
        elif movement_type == "fast_movers":
            mover_where = " AND sa.units_sold >= 3"
        elif movement_type == "stockout_risk":
            mover_where = " AND (tb.is_in_stock = 0 OR tb.total_stock < 5)"

        count_sql = f"""
            SELECT COUNT(*)
            FROM daily_inventory_snapshots tb
            JOIN daily_inventory_snapshots ta ON tb.product_id = ta.product_id AND ta.snapshot_date = ?
            LEFT JOIN daily_sales_analytics sa ON tb.product_id = sa.product_id AND sa.analytics_date = tb.snapshot_date
            WHERE tb.snapshot_date = ? {filter_clause} {mover_where};
        """
        cur.execute(count_sql, [date_a, date_b] + filter_params)
        movers_total = cur.fetchone()[0]

        offset = (page - 1) * per_page
        movers_sql = f"""
            SELECT 
                p.product_id, p.brand, p.title, p.category, p.product_url, p.mrp,
                ta.selling_price as yest_price, tb.selling_price as today_price,
                ROUND(tb.selling_price - ta.selling_price, 2) as price_delta,
                ROUND(((tb.selling_price - ta.selling_price) / ta.selling_price) * 100.0, 1) as price_delta_pct,
                ta.discount_percentage as yest_disc, tb.discount_percentage as today_disc,
                (tb.discount_percentage - ta.discount_percentage) as discount_delta,
                ta.total_stock as yest_stock, tb.total_stock as today_stock,
                (tb.total_stock - ta.total_stock) as stock_delta,
                COALESCE(sa.units_sold, 0) as units_sold,
                COALESCE(sa.revenue_generated, 0.0) as revenue,
                COALESCE(sa.stock_added, 0) as stock_added,
                ta.is_in_stock as yest_in_stock, tb.is_in_stock as today_in_stock,
                p.average_rating, p.total_ratings_count, p.primary_color, p.color_hex
            FROM daily_inventory_snapshots tb
            JOIN daily_inventory_snapshots ta ON tb.product_id = ta.product_id AND ta.snapshot_date = ?
            JOIN products p ON tb.product_id = p.product_id
            LEFT JOIN daily_sales_analytics sa ON tb.product_id = sa.product_id AND sa.analytics_date = tb.snapshot_date
            WHERE tb.snapshot_date = ? {filter_clause} {mover_where}
            ORDER BY ABS(tb.selling_price - ta.selling_price) DESC, COALESCE(sa.units_sold, 0) DESC
            LIMIT ? OFFSET ?;
        """
        cur.execute(movers_sql, [date_a, date_b] + filter_params + [per_page, offset])
        movers = []
        for r in cur.fetchall():
            movers.append({
                "product_id": r[0], "brand": r[1], "title": r[2], "category": r[3], "product_url": r[4], "mrp": r[5],
                "yesterday_price": r[6], "today_price": r[7], "price_delta": r[8], "price_delta_pct": r[9],
                "yesterday_discount": r[10], "today_discount": r[11], "discount_delta": r[12],
                "yesterday_stock": r[13], "today_stock": r[14], "stock_delta": r[15],
                "units_sold": r[16], "revenue": r[17], "stock_added": r[18],
                "yesterday_in_stock": bool(r[19]), "today_in_stock": bool(r[20]),
                "average_rating": r[21], "total_ratings_count": r[22], "primary_color": r[23], "color_hex": r[24]
            })

        return {
            "date_a": date_a,
            "date_b": date_b,
            "summary": summary,
            "category_shifts": category_shifts,
            "top_price_drops": top_price_drops,
            "top_fast_movers": top_fast_movers,
            "top_restocked": top_restocked,
            "movers": movers,
            "total_movers": movers_total,
            "page": page,
            "per_page": per_page
        }

    def get_size_inventory_analytics(self, category: str = None, brand: str = None) -> Dict[str, Any]:
        """Returns deep size-wise inventory metrics, stockout risk distribution, and category sizing split."""
        conn = self._get_connection()
        cur = conn.cursor()

        where_clause = ""
        params = []
        if category:
            where_clause += " AND LOWER(p.category) = LOWER(?)"
            params.append(category)
        if brand:
            where_clause += " AND LOWER(p.brand) LIKE LOWER(?)"
            params.append(f"%{brand}%")

        # 1. Overall Summary
        cur.execute(f"""
            SELECT 
                COUNT(*) as total_variants,
                COALESCE(SUM(s.inventory_count), 0) as total_units,
                SUM(CASE WHEN s.inventory_count > 0 THEN 1 ELSE 0 END) as in_stock_variants,
                SUM(CASE WHEN s.inventory_count = 0 THEN 1 ELSE 0 END) as oos_variants,
                SUM(CASE WHEN s.inventory_count BETWEEN 1 AND 2 THEN 1 ELSE 0 END) as low_stock_variants
            FROM product_sizes s
            JOIN products p ON s.product_id = p.product_id
            WHERE 1=1 {where_clause};
        """, params)
        sum_row = cur.fetchone()
        tot_variants = sum_row[0] or 0
        tot_units = sum_row[1] or 0
        in_stock_v = sum_row[2] or 0
        oos_v = sum_row[3] or 0
        low_stock_v = sum_row[4] or 0
        stockout_rate = round((oos_v * 100.0 / tot_variants), 1) if tot_variants > 0 else 0.0

        # 2. Size Distribution List
        cur.execute(f"""
            SELECT 
                s.size,
                COUNT(DISTINCT s.product_id) as product_count,
                COALESCE(SUM(s.inventory_count), 0) as total_units,
                SUM(CASE WHEN s.inventory_count = 0 THEN 1 ELSE 0 END) as oos_count,
                SUM(CASE WHEN s.inventory_count BETWEEN 1 AND 2 THEN 1 ELSE 0 END) as low_stock_count,
                ROUND(SUM(CASE WHEN s.inventory_count = 0 THEN 1.0 ELSE 0.0 END) * 100.0 / COUNT(*), 1) as stockout_rate_pct
            FROM product_sizes s
            JOIN products p ON s.product_id = p.product_id
            WHERE 1=1 {where_clause}
            GROUP BY s.size
            ORDER BY total_units DESC, product_count DESC
            LIMIT 25;
        """, params)
        size_distribution = []
        for r in cur.fetchall():
            share_pct = round((r[2] * 100.0 / tot_units), 1) if tot_units > 0 else 0.0
            size_distribution.append({
                "size": r[0],
                "product_count": r[1],
                "total_units": r[2],
                "oos_count": r[3],
                "low_stock_count": r[4],
                "stockout_rate_pct": r[5],
                "stock_share_pct": share_pct
            })

        # 3. Category / Silhouette matrix breakdown
        cur.execute(f"""
            SELECT 
                COALESCE(NULLIF(p.sub_category, ''), p.category, 'Casual Shirts') as category,
                s.size,
                COALESCE(SUM(s.inventory_count), 0) as total_units,
                COUNT(DISTINCT s.product_id) as prod_count,
                SUM(CASE WHEN s.inventory_count = 0 THEN 1 ELSE 0 END) as oos_count
            FROM product_sizes s
            JOIN products p ON s.product_id = p.product_id
            WHERE 1=1 {where_clause}
            GROUP BY COALESCE(NULLIF(p.sub_category, ''), p.category, 'Casual Shirts'), s.size
            HAVING COALESCE(SUM(s.inventory_count), 0) > 0
                OR SUM(CASE WHEN s.inventory_count = 0 THEN 1 ELSE 0 END) > 0
            ORDER BY category, total_units DESC;
        """, params)
        cat_matrix = {}
        for r in cur.fetchall():
            cat = r[0]
            if cat not in cat_matrix:
                cat_matrix[cat] = []
            if len(cat_matrix[cat]) < 8:
                cat_matrix[cat].append({
                    "size": r[1],
                    "units": r[2],
                    "products": r[3],
                    "oos": r[4]
                })

        return {
            "summary": {
                "total_variants_tracked": tot_variants,
                "total_warehouse_units": tot_units,
                "in_stock_variants": in_stock_v,
                "out_of_stock_variants": oos_v,
                "low_stock_variants": low_stock_v,
                "stockout_rate_pct": stockout_rate
            },
            "size_distribution": size_distribution,
            "category_matrix": cat_matrix
        }

    def log_scraper_run(self, run_type: str, status: str, total_items: int = 0, successful_items: int = 0, failed_items: int = 0, rate: float = 0.0, duration: float = 0.0, log_summary: str = "", run_id: Optional[str] = None) -> str:
        conn = self._get_connection()
        if not run_id:
            run_id = f"run_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{random.randint(100, 999)}"
        now = datetime.now().astimezone().isoformat()
        with conn:
            conn.execute("""
                INSERT INTO scraper_runs (
                    run_id, run_type, status, started_at, completed_at, total_items, successful_items, failed_items, rate_items_per_sec, duration_seconds, log_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    completed_at=excluded.completed_at,
                    total_items=excluded.total_items,
                    successful_items=excluded.successful_items,
                    failed_items=excluded.failed_items,
                    rate_items_per_sec=excluded.rate_items_per_sec,
                    duration_seconds=excluded.duration_seconds,
                    log_summary=excluded.log_summary;
            """, (
                run_id, run_type, status, now, now if status != "RUNNING" else None,
                total_items, successful_items, failed_items, rate, duration, log_summary
            ))
        return run_id

    def log_scraper_error(self, run_id: str, product_id: Optional[int], error_type: str, error_message: str):
        conn = self._get_connection()
        with conn:
            conn.execute("""
                INSERT INTO scraper_errors (run_id, product_id, error_type, error_message)
                VALUES (?, ?, ?, ?);
            """, (run_id, product_id, error_type, error_message))

    def log_scraper_event(self, run_id: str, level: str, section: str, message: str, payload: Optional[Dict[str, Any]] = None):
        conn = self._get_connection()
        payload = dict(payload or {})
        with conn:
            conn.execute("""
                INSERT INTO scraper_events (
                    run_id, level, section, brand, page, route, status, saved, seen, expected, attempt,
                    elapsed_seconds, elapsed_label, products_per_minute, message, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                run_id,
                level,
                section,
                payload.get("brand"),
                payload.get("page"),
                payload.get("route"),
                payload.get("status"),
                payload.get("saved"),
                payload.get("seen"),
                payload.get("expected"),
                payload.get("attempt"),
                payload.get("elapsed_seconds"),
                payload.get("elapsed_label"),
                payload.get("products_per_minute"),
                message,
                json.dumps(payload, ensure_ascii=False) if payload else None,
            ))

    def upsert_scraper_brand_progress(self, run_id: str, brand: str, fields: Dict[str, Any]):
        conn = self._get_connection()
        fields = dict(fields or {})
        routes_scraped = fields.get("routes_scraped")
        routes_scraped_json = json.dumps(routes_scraped, ensure_ascii=False) if routes_scraped is not None else None
        now = datetime.now().astimezone().isoformat()
        with conn:
            conn.execute("""
                INSERT INTO scraper_brand_progress (
                    run_id, brand, status, page, route, expected_brand_total, unique_target_products_seen,
                    saved_products, completion_ratio, route_count, duration_seconds, duration_label,
                    products_per_minute, last_message, started_at, finished_at, updated_at, routes_scraped_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, brand) DO UPDATE SET
                    status=excluded.status,
                    page=excluded.page,
                    route=excluded.route,
                    expected_brand_total=excluded.expected_brand_total,
                    unique_target_products_seen=excluded.unique_target_products_seen,
                    saved_products=excluded.saved_products,
                    completion_ratio=excluded.completion_ratio,
                    route_count=excluded.route_count,
                    duration_seconds=excluded.duration_seconds,
                    duration_label=excluded.duration_label,
                    products_per_minute=excluded.products_per_minute,
                    last_message=excluded.last_message,
                    started_at=COALESCE(scraper_brand_progress.started_at, excluded.started_at),
                    finished_at=COALESCE(excluded.finished_at, scraper_brand_progress.finished_at),
                    updated_at=excluded.updated_at,
                    routes_scraped_json=COALESCE(excluded.routes_scraped_json, scraper_brand_progress.routes_scraped_json);
            """, (
                run_id,
                brand,
                fields.get("status"),
                fields.get("page"),
                fields.get("route"),
                fields.get("expected_brand_total") or 0,
                fields.get("unique_target_products_seen") or fields.get("seen_products") or 0,
                fields.get("saved_products") or 0,
                fields.get("completion_ratio") or 0,
                fields.get("route_count") or 0,
                fields.get("duration_seconds") or fields.get("elapsed_seconds") or 0,
                fields.get("duration_label") or fields.get("elapsed_label"),
                fields.get("products_per_minute") or 0,
                fields.get("last_message"),
                fields.get("started_at"),
                fields.get("finished_at"),
                now,
                routes_scraped_json,
            ))

    def get_scraper_brand_progress(self, run_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT run_id, brand, status, page, route, expected_brand_total, unique_target_products_seen,
                   saved_products, completion_ratio, route_count, duration_seconds, duration_label,
                   products_per_minute, last_message, started_at, finished_at, updated_at, routes_scraped_json
            FROM scraper_brand_progress
            WHERE run_id = ?
            ORDER BY updated_at DESC, brand ASC
            LIMIT ?;
        """, (run_id, limit))
        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                "run_id": r[0],
                "brand": r[1],
                "status": r[2],
                "page": r[3],
                "route": r[4],
                "expected_brand_total": r[5],
                "unique_target_products_seen": r[6],
                "saved_products": r[7],
                "completion_ratio": r[8],
                "route_count": r[9],
                "duration_seconds": r[10],
                "duration_label": r[11],
                "products_per_minute": r[12],
                "last_message": r[13],
                "started_at": r[14],
                "finished_at": r[15],
                "updated_at": r[16],
                "routes_scraped": _safe_parse_json(r[17], default=[]),
            })
        return results

    def get_scraper_brand_run_history(
        self,
        limit: int = 250,
        status: Optional[str] = None,
        search: str = "",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        where_clauses = ["1=1"]
        params: List[Any] = []

        normalized_status = str(status or "").strip().upper()
        if normalized_status and normalized_status != "ALL":
            where_clauses.append("UPPER(COALESCE(bp.status, '')) = ?")
            params.append(normalized_status)

        search_token = str(search or "").strip()
        if search_token:
            like = f"%{search_token}%"
            where_clauses.append(
                "("
                "bp.brand LIKE ? OR "
                "bp.run_id LIKE ? OR "
                "bp.route LIKE ? OR "
                "bp.last_message LIKE ? OR "
                "bp.routes_scraped_json LIKE ?"
                ")"
            )
            params.extend([like, like, like, like, like])

        if start_date:
            where_clauses.append("DATE(COALESCE(bp.started_at, bp.updated_at, bp.finished_at)) >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("DATE(COALESCE(bp.started_at, bp.updated_at, bp.finished_at)) <= ?")
            params.append(end_date)

        params.append(max(1, min(int(limit or 250), 1000)))

        cur.execute(f"""
            WITH retry_counts AS (
                SELECT
                    run_id,
                    brand,
                    COUNT(*) AS retry_events
                FROM scraper_events
                WHERE brand IS NOT NULL
                  AND brand != ''
                  AND (
                    message LIKE 'Retrying route %'
                    OR message LIKE 'Audit retry %'
                    OR message LIKE 'Gateway listing call was rejected %'
                    OR message LIKE 'Route fetch failed %'
                  )
                GROUP BY run_id, brand
            )
            SELECT
                bp.run_id,
                bp.brand,
                bp.status,
                bp.route,
                bp.expected_brand_total,
                bp.unique_target_products_seen,
                bp.saved_products,
                bp.completion_ratio,
                bp.route_count,
                bp.duration_seconds,
                bp.duration_label,
                bp.products_per_minute,
                bp.last_message,
                bp.started_at,
                bp.finished_at,
                bp.updated_at,
                bp.routes_scraped_json,
                COALESCE(rc.retry_events, 0) + 1 AS attempts
            FROM scraper_brand_progress bp
            LEFT JOIN retry_counts rc
              ON rc.run_id = bp.run_id AND rc.brand = bp.brand
            WHERE {" AND ".join(where_clauses)}
            ORDER BY COALESCE(bp.updated_at, bp.finished_at, bp.started_at) DESC, bp.brand ASC
            LIMIT ?;
        """, params)
        rows = cur.fetchall()
        results = []
        for r in rows:
            routes = _safe_parse_json(r[16], default=[])
            primary_url = r[3] or ""
            if not primary_url and isinstance(routes, list):
                for route in routes:
                    if isinstance(route, dict) and route.get("url"):
                        primary_url = str(route.get("url"))
                        break
            slug = ""
            if primary_url:
                try:
                    parsed = urlparse(primary_url)
                    slug = parsed.path.strip("/").split("/")[0] if parsed.path else ""
                except Exception:
                    slug = ""
            results.append({
                "run_id": r[0],
                "brand": r[1],
                "status": r[2],
                "primary_url": primary_url,
                "store_slug": slug,
                "expected_brand_total": int(r[4] or 0),
                "unique_target_products_seen": int(r[5] or 0),
                "saved_products": int(r[6] or 0),
                "completion_ratio": float(r[7] or 0.0),
                "route_count": int(r[8] or 0),
                "duration_seconds": float(r[9] or 0.0),
                "duration_label": r[10],
                "products_per_minute": float(r[11] or 0.0),
                "last_message": r[12] or "",
                "started_at": r[13],
                "completed_at": r[14],
                "updated_at": r[15],
                "routes_scraped": routes,
                "attempts": int(r[17] or 1),
            })
        return results

    def get_scraper_brand_run_summary(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, int]:
        conn = self._get_connection()
        cur = conn.cursor()
        where_clauses = ["1=1"]
        params: List[Any] = []
        if start_date:
            where_clauses.append("DATE(COALESCE(started_at, updated_at, finished_at)) >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("DATE(COALESCE(started_at, updated_at, finished_at)) <= ?")
            params.append(end_date)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total_runs,
                COALESCE(SUM(CASE WHEN UPPER(COALESCE(status, '')) = 'RUNNING' THEN 1 ELSE 0 END), 0) AS running_count,
                COALESCE(SUM(CASE WHEN UPPER(COALESCE(status, '')) = 'COMPLETED' THEN 1 ELSE 0 END), 0) AS completed_count,
                COALESCE(SUM(CASE WHEN UPPER(COALESCE(status, '')) = 'FAILED' THEN 1 ELSE 0 END), 0) AS failed_count
            FROM scraper_brand_progress
            WHERE {" AND ".join(where_clauses)};
        """, params)
        row = cur.fetchone() or {}
        return {
            "total_runs": int(row["total_runs"] or 0),
            "running_count": int(row["running_count"] or 0),
            "completed_count": int(row["completed_count"] or 0),
            "failed_count": int(row["failed_count"] or 0),
        }

    def get_scraper_history_dates(self, limit: int = 60) -> List[str]:
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT run_day
            FROM (
                SELECT DISTINCT DATE(COALESCE(started_at, updated_at, finished_at)) AS run_day
                FROM scraper_brand_progress
                WHERE COALESCE(started_at, updated_at, finished_at) IS NOT NULL

                UNION

                SELECT DISTINCT DATE(snapshot_date) AS run_day
                FROM daily_inventory_snapshots
                WHERE snapshot_date IS NOT NULL

                UNION

                SELECT DISTINCT DATE(analytics_date) AS run_day
                FROM daily_sales_analytics
                WHERE analytics_date IS NOT NULL
            ) dated_points
            WHERE run_day IS NOT NULL
            ORDER BY run_day DESC
            LIMIT ?;
        """, (max(1, min(int(limit or 60), 365)),))
        return [str(r[0]) for r in cur.fetchall() if r and r[0]]

    def get_scraper_brand_run_daily_summary(
        self,
        limit: int = 30,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        where_clauses = ["1=1"]
        params: List[Any] = []
        if start_date:
            where_clauses.append("DATE(COALESCE(started_at, updated_at, finished_at)) >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("DATE(COALESCE(started_at, updated_at, finished_at)) <= ?")
            params.append(end_date)
        params.append(max(1, min(int(limit or 30), 180)))
        cur.execute(f"""
            SELECT
                DATE(COALESCE(started_at, updated_at, finished_at)) AS run_day,
                COUNT(*) AS total_runs,
                COALESCE(SUM(saved_products), 0) AS total_saved_products,
                COALESCE(SUM(expected_brand_total), 0) AS total_expected_products,
                COALESCE(SUM(unique_target_products_seen), 0) AS total_seen_products,
                COALESCE(AVG(duration_seconds), 0) AS avg_duration_seconds,
                COALESCE(SUM(CASE WHEN UPPER(COALESCE(status, '')) = 'RUNNING' THEN 1 ELSE 0 END), 0) AS running_count,
                COALESCE(SUM(CASE WHEN UPPER(COALESCE(status, '')) = 'COMPLETED' THEN 1 ELSE 0 END), 0) AS completed_count,
                COALESCE(SUM(CASE WHEN UPPER(COALESCE(status, '')) = 'FAILED' THEN 1 ELSE 0 END), 0) AS failed_count
            FROM scraper_brand_progress
            WHERE {" AND ".join(where_clauses)}
            GROUP BY run_day
            ORDER BY run_day DESC
            LIMIT ?;
        """, params)
        rows = cur.fetchall()
        return [
            {
                "run_day": str(r[0]),
                "total_runs": int(r[1] or 0),
                "total_saved_products": int(r[2] or 0),
                "total_expected_products": int(r[3] or 0),
                "total_seen_products": int(r[4] or 0),
                "avg_duration_seconds": float(r[5] or 0.0),
                "running_count": int(r[6] or 0),
                "completed_count": int(r[7] or 0),
                "failed_count": int(r[8] or 0),
            }
            for r in rows
        ]

    def get_scraper_logs_business_intelligence(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        brand_limit: int = 20,
        product_limit: int = 40,
    ) -> Dict[str, Any]:
        conn = self._get_connection()
        cur = conn.cursor()

        analytics_where = ["1=1"]
        analytics_params: List[Any] = []
        snapshot_where = ["1=1"]
        snapshot_params: List[Any] = []

        if start_date:
            analytics_where.append("DATE(analytics_date) >= ?")
            analytics_params.append(start_date)
            snapshot_where.append("DATE(snapshot_date) >= ?")
            snapshot_params.append(start_date)
        if end_date:
            analytics_where.append("DATE(analytics_date) <= ?")
            analytics_params.append(end_date)
            snapshot_where.append("DATE(snapshot_date) <= ?")
            snapshot_params.append(end_date)
        if not start_date and not end_date:
            analytics_where.append("analytics_date >= (CURRENT_TIMESTAMP - INTERVAL '14 days')")
            snapshot_where.append("snapshot_date >= (CURRENT_TIMESTAMP - INTERVAL '14 days')")

        analytics_where_sql = " AND ".join(analytics_where)
        snapshot_where_sql = " AND ".join(snapshot_where)

        cur.execute(f"""
            WITH filtered_sales AS (
                SELECT
                    analytics_date,
                    DATE(analytics_date) AS sales_day,
                    product_id,
                    brand,
                    category,
                    COALESCE(units_sold, 0) AS units_sold,
                    COALESCE(revenue_generated, 0) AS revenue_generated,
                    COALESCE(stock_added, 0) AS stock_added
                FROM daily_sales_analytics
                WHERE {analytics_where_sql}
            ),
            latest_snapshot AS (
                SELECT MAX(snapshot_date) AS latest_snapshot_at
                FROM daily_inventory_snapshots
                WHERE {snapshot_where_sql}
            ),
            latest_stock AS (
                SELECT
                    COUNT(*) FILTER (WHERE COALESCE(s.total_stock, 0) > 0 AND COALESCE(s.is_in_stock, 0) = 1) AS in_stock_products,
                    COUNT(*) FILTER (WHERE COALESCE(s.total_stock, 0) <= 0 OR COALESCE(s.is_in_stock, 0) = 0) AS oos_products,
                    COALESCE(SUM(COALESCE(s.total_stock, 0)), 0) AS inventory_units
                FROM daily_inventory_snapshots s
                JOIN latest_snapshot ls ON ls.latest_snapshot_at = s.snapshot_date
            ),
            day_snapshots AS (
                SELECT DATE(snapshot_date) AS snapshot_day, MAX(snapshot_date) AS latest_snapshot_at
                FROM daily_inventory_snapshots
                WHERE {snapshot_where_sql}
                GROUP BY DATE(snapshot_date)
            ),
            day_pairs AS (
                SELECT
                    snapshot_day,
                    latest_snapshot_at,
                    LAG(latest_snapshot_at) OVER (ORDER BY snapshot_day) AS prior_snapshot_at
                FROM day_snapshots
            ),
            transition_totals AS (
                SELECT
                    COALESCE(SUM(CASE WHEN COALESCE(prev.is_in_stock, 0) = 1 AND COALESCE(curr.is_in_stock, 0) = 0 THEN 1 ELSE 0 END), 0) AS went_oos_count,
                    COALESCE(SUM(CASE WHEN COALESCE(prev.is_in_stock, 0) = 0 AND COALESCE(curr.is_in_stock, 0) = 1 THEN 1 ELSE 0 END), 0) AS back_in_stock_count
                FROM day_pairs dp
                JOIN daily_inventory_snapshots curr ON curr.snapshot_date = dp.latest_snapshot_at
                LEFT JOIN daily_inventory_snapshots prev
                  ON prev.product_id = curr.product_id
                 AND prev.snapshot_date = dp.prior_snapshot_at
                WHERE dp.prior_snapshot_at IS NOT NULL
            )
            SELECT
                COALESCE(SUM(fs.units_sold), 0) AS total_units_sold,
                COALESCE(SUM(fs.revenue_generated), 0) AS total_revenue,
                COALESCE(SUM(fs.stock_added), 0) AS total_stock_added,
                COUNT(DISTINCT fs.product_id) AS touched_skus,
                COUNT(DISTINCT fs.brand) AS active_brands,
                COALESCE((SELECT in_stock_products FROM latest_stock), 0) AS in_stock_products,
                COALESCE((SELECT oos_products FROM latest_stock), 0) AS oos_products,
                COALESCE((SELECT inventory_units FROM latest_stock), 0) AS inventory_units,
                COALESCE((SELECT went_oos_count FROM transition_totals), 0) AS went_oos_count,
                COALESCE((SELECT back_in_stock_count FROM transition_totals), 0) AS back_in_stock_count,
                COALESCE((SELECT latest_snapshot_at FROM latest_snapshot), NULL) AS latest_snapshot_at
            FROM filtered_sales fs;
        """, analytics_params + snapshot_params + snapshot_params)
        summary_row = cur.fetchone() or {}

        cur.execute(f"""
            WITH filtered_sales AS (
                SELECT
                    DATE(analytics_date) AS sales_day,
                    COALESCE(units_sold, 0) AS units_sold,
                    COALESCE(revenue_generated, 0) AS revenue_generated,
                    COALESCE(stock_added, 0) AS stock_added,
                    product_id
                FROM daily_sales_analytics
                WHERE {analytics_where_sql}
            ),
            daily_sales AS (
                SELECT
                    sales_day,
                    COALESCE(SUM(units_sold), 0) AS units_sold,
                    COALESCE(SUM(revenue_generated), 0) AS revenue_generated,
                    COALESCE(SUM(stock_added), 0) AS stock_added,
                    COUNT(DISTINCT CASE WHEN units_sold > 0 OR stock_added > 0 THEN product_id END) AS touched_skus
                FROM filtered_sales
                GROUP BY sales_day
            ),
            day_snapshots AS (
                SELECT DATE(snapshot_date) AS snapshot_day, MAX(snapshot_date) AS latest_snapshot_at
                FROM daily_inventory_snapshots
                WHERE {snapshot_where_sql}
                GROUP BY DATE(snapshot_date)
            ),
            daily_stock AS (
                SELECT
                    ds.snapshot_day,
                    COUNT(*) FILTER (WHERE COALESCE(s.total_stock, 0) > 0 AND COALESCE(s.is_in_stock, 0) = 1) AS in_stock_products,
                    COUNT(*) FILTER (WHERE COALESCE(s.total_stock, 0) <= 0 OR COALESCE(s.is_in_stock, 0) = 0) AS oos_products,
                    COALESCE(SUM(COALESCE(s.total_stock, 0)), 0) AS inventory_units
                FROM day_snapshots ds
                JOIN daily_inventory_snapshots s ON s.snapshot_date = ds.latest_snapshot_at
                GROUP BY ds.snapshot_day
            ),
            day_pairs AS (
                SELECT
                    snapshot_day,
                    latest_snapshot_at,
                    LAG(latest_snapshot_at) OVER (ORDER BY snapshot_day) AS prior_snapshot_at
                FROM day_snapshots
            ),
            daily_transitions AS (
                SELECT
                    dp.snapshot_day,
                    COALESCE(SUM(CASE WHEN COALESCE(prev.is_in_stock, 0) = 1 AND COALESCE(curr.is_in_stock, 0) = 0 THEN 1 ELSE 0 END), 0) AS went_oos_count,
                    COALESCE(SUM(CASE WHEN COALESCE(prev.is_in_stock, 0) = 0 AND COALESCE(curr.is_in_stock, 0) = 1 THEN 1 ELSE 0 END), 0) AS back_in_stock_count
                FROM day_pairs dp
                JOIN daily_inventory_snapshots curr ON curr.snapshot_date = dp.latest_snapshot_at
                LEFT JOIN daily_inventory_snapshots prev
                  ON prev.product_id = curr.product_id
                 AND prev.snapshot_date = dp.prior_snapshot_at
                WHERE dp.prior_snapshot_at IS NOT NULL
                GROUP BY dp.snapshot_day
            ),
            all_days AS (
                SELECT sales_day AS day_key FROM daily_sales
                UNION
                SELECT snapshot_day AS day_key FROM daily_stock
                UNION
                SELECT snapshot_day AS day_key FROM daily_transitions
            )
            SELECT
                ad.day_key,
                COALESCE(ds.units_sold, 0) AS units_sold,
                COALESCE(ds.revenue_generated, 0) AS revenue_generated,
                COALESCE(ds.stock_added, 0) AS stock_added,
                COALESCE(ds.touched_skus, 0) AS touched_skus,
                COALESCE(st.in_stock_products, 0) AS in_stock_products,
                COALESCE(st.oos_products, 0) AS oos_products,
                COALESCE(st.inventory_units, 0) AS inventory_units,
                COALESCE(tr.went_oos_count, 0) AS went_oos_count,
                COALESCE(tr.back_in_stock_count, 0) AS back_in_stock_count
            FROM all_days ad
            LEFT JOIN daily_sales ds ON ds.sales_day = ad.day_key
            LEFT JOIN daily_stock st ON st.snapshot_day = ad.day_key
            LEFT JOIN daily_transitions tr ON tr.snapshot_day = ad.day_key
            ORDER BY ad.day_key DESC;
        """, analytics_params + snapshot_params)
        daily_rows = cur.fetchall()

        cur.execute(f"""
            WITH filtered_sales AS (
                SELECT
                    brand,
                    product_id,
                    COALESCE(units_sold, 0) AS units_sold,
                    COALESCE(revenue_generated, 0) AS revenue_generated,
                    COALESCE(stock_added, 0) AS stock_added
                FROM daily_sales_analytics
                WHERE {analytics_where_sql}
            ),
            brand_sales AS (
                SELECT
                    brand,
                    COUNT(DISTINCT product_id) AS touched_skus,
                    COALESCE(SUM(units_sold), 0) AS units_sold,
                    COALESCE(SUM(revenue_generated), 0) AS revenue_generated,
                    COALESCE(SUM(stock_added), 0) AS stock_added
                FROM filtered_sales
                GROUP BY brand
            ),
            latest_snapshot AS (
                SELECT MAX(snapshot_date) AS latest_snapshot_at
                FROM daily_inventory_snapshots
                WHERE {snapshot_where_sql}
            ),
            brand_stock AS (
                SELECT
                    s.brand,
                    COUNT(*) FILTER (WHERE COALESCE(s.total_stock, 0) > 0 AND COALESCE(s.is_in_stock, 0) = 1) AS in_stock_products,
                    COUNT(*) FILTER (WHERE COALESCE(s.total_stock, 0) <= 0 OR COALESCE(s.is_in_stock, 0) = 0) AS oos_products,
                    COALESCE(SUM(COALESCE(s.total_stock, 0)), 0) AS inventory_units
                FROM daily_inventory_snapshots s
                JOIN latest_snapshot ls ON ls.latest_snapshot_at = s.snapshot_date
                GROUP BY s.brand
            ),
            brands_union AS (
                SELECT brand FROM brand_sales
                UNION
                SELECT brand FROM brand_stock
            )
            SELECT
                bu.brand,
                COALESCE(bs.touched_skus, 0) AS touched_skus,
                COALESCE(bs.units_sold, 0) AS units_sold,
                COALESCE(bs.revenue_generated, 0) AS revenue_generated,
                COALESCE(bs.stock_added, 0) AS stock_added,
                COALESCE(bst.in_stock_products, 0) AS in_stock_products,
                COALESCE(bst.oos_products, 0) AS oos_products,
                COALESCE(bst.inventory_units, 0) AS inventory_units
            FROM brands_union bu
            LEFT JOIN brand_sales bs ON bs.brand = bu.brand
            LEFT JOIN brand_stock bst ON bst.brand = bu.brand
            WHERE bu.brand IS NOT NULL AND TRIM(bu.brand) != ''
            ORDER BY COALESCE(bs.revenue_generated, 0) DESC, COALESCE(bs.units_sold, 0) DESC, COALESCE(bst.inventory_units, 0) DESC, bu.brand ASC
            LIMIT ?;
        """, analytics_params + snapshot_params + [max(1, min(int(brand_limit or 20), 100))])
        brand_rows = cur.fetchall()

        cur.execute(f"""
            WITH product_sales AS (
                SELECT
                    sa.product_id,
                    MAX(sa.analytics_date) AS last_seen_at,
                    MAX(sa.brand) AS brand,
                    MAX(sa.category) AS category,
                    COALESCE(SUM(sa.units_sold), 0) AS units_sold,
                    COALESCE(SUM(sa.revenue_generated), 0) AS revenue_generated,
                    COALESCE(SUM(sa.stock_added), 0) AS stock_added
                FROM daily_sales_analytics sa
                WHERE {analytics_where_sql}
                GROUP BY sa.product_id
            ),
            latest_snapshot AS (
                SELECT MAX(snapshot_date) AS latest_snapshot_at
                FROM daily_inventory_snapshots
                WHERE {snapshot_where_sql}
            ),
            latest_product_stock AS (
                SELECT
                    s.product_id,
                    COALESCE(s.total_stock, 0) AS total_stock,
                    COALESCE(s.is_in_stock, 0) AS is_in_stock
                FROM daily_inventory_snapshots s
                JOIN latest_snapshot ls ON ls.latest_snapshot_at = s.snapshot_date
            )
            SELECT
                ps.last_seen_at,
                ps.product_id,
                COALESCE(p.brand, ps.brand) AS brand,
                COALESCE(p.title, '') AS title,
                COALESCE(p.category, ps.category) AS category,
                COALESCE(p.selling_price, 0) AS selling_price,
                COALESCE(p.product_url, '') AS product_url,
                ps.units_sold,
                ps.revenue_generated,
                ps.stock_added,
                COALESCE(lps.total_stock, 0) AS total_stock,
                COALESCE(lps.is_in_stock, 0) AS is_in_stock
            FROM product_sales ps
            JOIN products p ON p.product_id = ps.product_id
            LEFT JOIN latest_product_stock lps ON lps.product_id = ps.product_id
            ORDER BY ps.units_sold DESC, ps.revenue_generated DESC, ps.stock_added DESC, ps.last_seen_at DESC
            LIMIT ?;
        """, analytics_params + snapshot_params + [max(1, min(int(product_limit or 40), 200))])
        product_rows = cur.fetchall()

        return {
            "summary": {
                "total_units_sold": int(summary_row["total_units_sold"] or 0),
                "total_revenue": round(float(summary_row["total_revenue"] or 0.0), 2),
                "total_stock_added": int(summary_row["total_stock_added"] or 0),
                "touched_skus": int(summary_row["touched_skus"] or 0),
                "active_brands": int(summary_row["active_brands"] or 0),
                "in_stock_products": int(summary_row["in_stock_products"] or 0),
                "oos_products": int(summary_row["oos_products"] or 0),
                "inventory_units": int(summary_row["inventory_units"] or 0),
                "went_oos_count": int(summary_row["went_oos_count"] or 0),
                "back_in_stock_count": int(summary_row["back_in_stock_count"] or 0),
                "latest_snapshot_at": summary_row["latest_snapshot_at"],
            },
            "daily_rows": [
                {
                    "day": str(r["day_key"]),
                    "units_sold": int(r["units_sold"] or 0),
                    "revenue_generated": round(float(r["revenue_generated"] or 0.0), 2),
                    "stock_added": int(r["stock_added"] or 0),
                    "touched_skus": int(r["touched_skus"] or 0),
                    "in_stock_products": int(r["in_stock_products"] or 0),
                    "oos_products": int(r["oos_products"] or 0),
                    "inventory_units": int(r["inventory_units"] or 0),
                    "went_oos_count": int(r["went_oos_count"] or 0),
                    "back_in_stock_count": int(r["back_in_stock_count"] or 0),
                }
                for r in daily_rows
            ],
            "brand_rows": [
                {
                    "brand": r["brand"],
                    "touched_skus": int(r["touched_skus"] or 0),
                    "units_sold": int(r["units_sold"] or 0),
                    "revenue_generated": round(float(r["revenue_generated"] or 0.0), 2),
                    "stock_added": int(r["stock_added"] or 0),
                    "in_stock_products": int(r["in_stock_products"] or 0),
                    "oos_products": int(r["oos_products"] or 0),
                    "inventory_units": int(r["inventory_units"] or 0),
                }
                for r in brand_rows
            ],
            "product_rows": [
                {
                    "last_seen_at": r["last_seen_at"],
                    "product_id": r["product_id"],
                    "brand": r["brand"],
                    "title": r["title"],
                    "category": r["category"],
                    "selling_price": round(float(r["selling_price"] or 0.0), 2),
                    "product_url": r["product_url"],
                    "units_sold": int(r["units_sold"] or 0),
                    "revenue_generated": round(float(r["revenue_generated"] or 0.0), 2),
                    "stock_added": int(r["stock_added"] or 0),
                    "total_stock": int(r["total_stock"] or 0),
                    "is_in_stock": bool(r["is_in_stock"]),
                }
                for r in product_rows
            ],
        }

    def get_scraper_event_section_counts(self, limit: int = 500) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT section, COUNT(*) AS cnt
            FROM (
                SELECT section
                FROM scraper_events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            ) recent_events
            GROUP BY section
            ORDER BY cnt DESC, section ASC;
        """, (max(1, min(int(limit or 500), 5000)),))
        rows = cur.fetchall()
        return [
            {
                "section": r[0] or "general",
                "count": int(r[1] or 0),
            }
            for r in rows
        ]

    def get_scraper_events_feed(
        self,
        limit: int = 200,
        section: Optional[str] = None,
        level: Optional[str] = None,
        search: str = "",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        where_clauses = ["1=1"]
        params: List[Any] = []

        if section:
            where_clauses.append("LOWER(COALESCE(section, '')) = LOWER(?)")
            params.append(section)
        if level:
            where_clauses.append("UPPER(COALESCE(level, '')) = UPPER(?)")
            params.append(level)
        if search:
            like = f"%{search}%"
            where_clauses.append(
                "("
                "message LIKE ? OR "
                "brand LIKE ? OR "
                "route LIKE ? OR "
                "run_id LIKE ?"
                ")"
            )
            params.extend([like, like, like, like])
        if start_date:
            where_clauses.append("DATE(created_at) >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("DATE(created_at) <= ?")
            params.append(end_date)

        params.append(max(1, min(int(limit or 200), 2000)))
        cur.execute(f"""
            SELECT id, run_id, level, section, brand, page, route, status, saved, seen, expected, attempt,
                   elapsed_seconds, elapsed_label, products_per_minute, message, payload_json, created_at
            FROM scraper_events
            WHERE {" AND ".join(where_clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?;
        """, params)
        rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "run_id": r[1],
                "level": r[2],
                "section": r[3],
                "brand": r[4],
                "page": r[5],
                "route": r[6],
                "status": r[7],
                "saved": r[8],
                "seen": r[9],
                "expected": r[10],
                "attempt": r[11],
                "elapsed_seconds": r[12],
                "elapsed_label": r[13],
                "products_per_minute": r[14],
                "message": r[15],
                "payload": _safe_parse_json(r[16], default={}),
                "created_at": r[17],
            }
            for r in rows
        ]

    def get_scraper_audit_gaps(
        self,
        limit: int = 200,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        status: Optional[str] = None,
        search: str = "",
    ) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        where_clauses = [
            "(COALESCE(expected_brand_total, 0) > COALESCE(unique_target_products_seen, 0) "
            "OR UPPER(COALESCE(status, '')) = 'FAILED')"
        ]
        params: List[Any] = []
        if start_date:
            where_clauses.append("DATE(COALESCE(started_at, updated_at, finished_at)) >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("DATE(COALESCE(started_at, updated_at, finished_at)) <= ?")
            params.append(end_date)
        if status and status.upper() != "ALL":
            where_clauses.append("UPPER(COALESCE(status, '')) = ?")
            params.append(status.upper())
        if search:
            like = f"%{search}%"
            where_clauses.append("(brand LIKE ? OR route LIKE ? OR run_id LIKE ? OR last_message LIKE ?)")
            params.extend([like, like, like, like])
        params.append(max(1, min(int(limit or 200), 1000)))
        cur.execute(f"""
            SELECT
                run_id,
                brand,
                status,
                route,
                expected_brand_total,
                unique_target_products_seen,
                saved_products,
                duration_label,
                last_message,
                started_at,
                finished_at,
                updated_at
            FROM scraper_brand_progress
            WHERE {" AND ".join(where_clauses)}
            ORDER BY (COALESCE(expected_brand_total, 0) - COALESCE(unique_target_products_seen, 0)) DESC,
                     COALESCE(updated_at, finished_at, started_at) DESC
            LIMIT ?;
        """, params)
        rows = cur.fetchall()
        results = []
        for r in rows:
            expected = int(r[4] or 0)
            seen = int(r[5] or 0)
            gap = max(0, expected - seen)
            coverage_pct = round((seen / expected) * 100.0, 1) if expected > 0 else 0.0
            results.append({
                "run_id": r[0],
                "brand": r[1],
                "status": r[2],
                "route": r[3],
                "expected_brand_total": expected,
                "unique_target_products_seen": seen,
                "saved_products": int(r[6] or 0),
                "duration_label": r[7],
                "last_message": r[8] or "",
                "started_at": r[9],
                "completed_at": r[10],
                "updated_at": r[11],
                "gap_count": gap,
                "coverage_pct": coverage_pct,
            })
        return results

    def get_scraper_events(self, run_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, run_id, level, section, brand, page, route, status, saved, seen, expected, attempt,
                   elapsed_seconds, elapsed_label, products_per_minute, message, payload_json, created_at
            FROM scraper_events
            WHERE run_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?;
        """, (run_id, limit))
        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                "id": r[0],
                "run_id": r[1],
                "level": r[2],
                "section": r[3],
                "brand": r[4],
                "page": r[5],
                "route": r[6],
                "status": r[7],
                "saved": r[8],
                "seen": r[9],
                "expected": r[10],
                "attempt": r[11],
                "elapsed_seconds": r[12],
                "elapsed_label": r[13],
                "products_per_minute": r[14],
                "message": r[15],
                "payload": _safe_parse_json(r[16], default={}),
                "created_at": r[17],
            })
        return results

    def get_scraper_runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT run_id, run_type, status, started_at, completed_at, total_items, successful_items, failed_items, rate_items_per_sec, duration_seconds, log_summary
            FROM scraper_runs
            ORDER BY started_at DESC
            LIMIT ?;
        """, (limit,))
        rows = cur.fetchall()
        if not rows:
            return []

        runs = []
        for r in rows:
            runs.append({
                "run_id": r[0],
                "run_type": r[1],
                "status": r[2],
                "started_at": r[3],
                "completed_at": r[4],
                "total_items": r[5],
                "successful_items": r[6],
                "failed_items": r[7],
                "rate_items_per_sec": r[8],
                "duration_seconds": r[9],
                "log_summary": r[10]
            })
        return runs

    def get_scraper_errors(self, run_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        if run_id:
            cur.execute("""
                SELECT id, run_id, product_id, error_type, error_message, created_at
                FROM scraper_errors
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT ?;
            """, (run_id, limit))
        else:
            cur.execute("""
                SELECT id, run_id, product_id, error_type, error_message, created_at
                FROM scraper_errors
                ORDER BY id DESC
                LIMIT ?;
            """, (limit,))
        rows = cur.fetchall()
        if not rows:
            return []
        errs = []
        for r in rows:
            errs.append({
                "id": r[0],
                "run_id": r[1],
                "product_id": r[2],
                "error_type": r[3],
                "error_message": r[4],
                "created_at": r[5]
            })
        return errs
