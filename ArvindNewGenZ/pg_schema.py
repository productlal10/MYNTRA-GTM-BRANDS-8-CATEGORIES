"""
pg_schema.py
Create PostgreSQL schema + indexes for the Myntra catalog.
Run once: python3 pg_schema.py
"""

import psycopg2
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

PG_DSN = {
    "host": os.getenv("PG_HOST", "127.0.0.1"),
    "port": int(os.getenv("PG_PORT", 5432)),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
    "dbname": os.getenv("PG_DBNAME", "arvind_new_genz_shirts"),
}

DDL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ──────────────────────────────────────────────────────────────
-- Products — core catalog
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    product_id          BIGINT PRIMARY KEY,
    sku                 TEXT,
    brand               TEXT,
    title               TEXT,
    category            TEXT,
    sub_category        TEXT,
    gender              TEXT,
    product_url         TEXT,
    mrp                 NUMERIC(12,2),
    selling_price       NUMERIC(12,2),
    discount_percentage INTEGER,
    is_in_stock         SMALLINT DEFAULT 1,
    is_myntra_label     SMALLINT DEFAULT 0,
    brand_type          TEXT,
    fit                 TEXT,
    fabric              TEXT,
    pattern             TEXT,
    primary_color       TEXT,
    color_hex           TEXT,
    average_rating      NUMERIC(3,1),
    total_ratings_count INTEGER,
    total_reviews_count INTEGER,
    primary_image_url   TEXT,
    current_total_stock INTEGER DEFAULT 0,
    current_available_size_count INTEGER DEFAULT 0,
    full_data_json      TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────
-- Product sizes / inventory
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS product_sizes (
    id              BIGSERIAL PRIMARY KEY,
    product_id      BIGINT REFERENCES products(product_id) ON DELETE CASCADE,
    size            TEXT,
    sku_id          BIGINT,
    available       SMALLINT,
    inventory_count INTEGER,
    raw_inventory_count INTEGER,
    inventory_quality TEXT
);

-- ──────────────────────────────────────────────────────────────
-- Brands metadata
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS brands (
    brand_name      TEXT PRIMARY KEY,
    ethnic_count    INTEGER DEFAULT 0,
    total_count     INTEGER DEFAULT 0,
    is_myntra_label SMALLINT DEFAULT 0,
    brand_type      TEXT,
    crawled_status  TEXT DEFAULT 'PENDING',
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────
-- Crawl state tracker
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crawl_state (
    category        TEXT,
    brand           TEXT,
    page            INTEGER,
    items_scraped   INTEGER DEFAULT 0,
    status          TEXT,
    last_scraped_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (category, brand)
);

-- ──────────────────────────────────────────────────────────────
-- Daily inventory snapshots
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_inventory_snapshots (
    snapshot_date       TIMESTAMPTZ,
    product_id          BIGINT,
    brand               TEXT,
    category            TEXT,
    selling_price       NUMERIC(12,2),
    mrp                 NUMERIC(12,2),
    discount_percentage INTEGER,
    is_in_stock         SMALLINT,
    total_stock         INTEGER,
    PRIMARY KEY (snapshot_date, product_id)
);

-- ──────────────────────────────────────────────────────────────
-- Daily sales analytics
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_sales_analytics (
    analytics_date      TIMESTAMPTZ,
    product_id          BIGINT,
    brand               TEXT,
    category            TEXT,
    units_sold          INTEGER DEFAULT 0,
    revenue_generated   NUMERIC(14,2) DEFAULT 0,
    stock_added         INTEGER DEFAULT 0,
    price_delta         NUMERIC(10,2) DEFAULT 0,
    ros                 NUMERIC(8,4) DEFAULT 0,
    stock_status        TEXT DEFAULT 'HEALTHY',
    PRIMARY KEY (analytics_date, product_id)
);

-- ──────────────────────────────────────────────────────────────
-- Scraper run logs
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scraper_runs (
    run_id              TEXT PRIMARY KEY,
    run_type            TEXT,
    status              TEXT,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    total_items         INTEGER DEFAULT 0,
    successful_items    INTEGER DEFAULT 0,
    failed_items        INTEGER DEFAULT 0,
    rate_items_per_sec  NUMERIC(8,2) DEFAULT 0,
    duration_seconds    NUMERIC(10,2) DEFAULT 0,
    log_summary         TEXT
);

CREATE TABLE IF NOT EXISTS scraper_errors (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT,
    product_id      BIGINT,
    error_type      TEXT,
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scraper_events (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT NOT NULL,
    level               TEXT,
    section             TEXT,
    brand               TEXT,
    page                INTEGER,
    route               TEXT,
    status              TEXT,
    saved               INTEGER,
    seen                INTEGER,
    expected            INTEGER,
    attempt             TEXT,
    elapsed_seconds     NUMERIC(10,2),
    elapsed_label       TEXT,
    products_per_minute NUMERIC(10,2),
    message             TEXT,
    payload_json        TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scraper_brand_progress (
    run_id                  TEXT NOT NULL,
    brand                   TEXT NOT NULL,
    status                  TEXT,
    page                    INTEGER,
    route                   TEXT,
    expected_brand_total    INTEGER DEFAULT 0,
    unique_target_products_seen INTEGER DEFAULT 0,
    saved_products          INTEGER DEFAULT 0,
    completion_ratio        NUMERIC(8,4) DEFAULT 0,
    route_count             INTEGER DEFAULT 0,
    duration_seconds        NUMERIC(10,2) DEFAULT 0,
    duration_label          TEXT,
    products_per_minute     NUMERIC(10,2) DEFAULT 0,
    last_message            TEXT,
    started_at              TIMESTAMPTZ,
    finished_at             TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    routes_scraped_json     TEXT,
    PRIMARY KEY (run_id, brand)
);

-- ──────────────────────────────────────────────────────────────
-- Indexes — optimized for the exact query patterns in server.py
-- ──────────────────────────────────────────────────────────────

-- products — single column
CREATE INDEX IF NOT EXISTS idx_pg_products_brand       ON products(brand);
CREATE INDEX IF NOT EXISTS idx_pg_products_cat         ON products(category);
CREATE INDEX IF NOT EXISTS idx_pg_products_subcat      ON products(sub_category);
CREATE INDEX IF NOT EXISTS idx_pg_products_gender      ON products(gender);
CREATE INDEX IF NOT EXISTS idx_pg_products_price       ON products(selling_price);
CREATE INDEX IF NOT EXISTS idx_pg_products_rating      ON products(average_rating);
CREATE INDEX IF NOT EXISTS idx_pg_products_fabric      ON products(fabric);
CREATE INDEX IF NOT EXISTS idx_pg_products_pattern     ON products(pattern);
CREATE INDEX IF NOT EXISTS idx_pg_products_color       ON products(primary_color);
CREATE INDEX IF NOT EXISTS idx_pg_products_fit         ON products(fit);
CREATE INDEX IF NOT EXISTS idx_pg_products_in_stock    ON products(is_in_stock);
CREATE INDEX IF NOT EXISTS idx_pg_products_discount    ON products(discount_percentage DESC);
CREATE INDEX IF NOT EXISTS idx_pg_products_myntra_lbl  ON products(is_myntra_label);

-- products — composite (matches GROUP BY + filter patterns in analytics queries)
CREATE INDEX IF NOT EXISTS idx_pg_products_cat_gender  ON products(category, gender);
CREATE INDEX IF NOT EXISTS idx_pg_products_cat_price   ON products(category, selling_price);
CREATE INDEX IF NOT EXISTS idx_pg_products_cat_brand   ON products(category, brand);
CREATE INDEX IF NOT EXISTS idx_pg_products_cat_disc    ON products(category, discount_percentage);
CREATE INDEX IF NOT EXISTS idx_pg_products_brand_price ON products(brand, selling_price);
CREATE INDEX IF NOT EXISTS idx_pg_products_brand_cat_gender ON products(brand, category, gender);
CREATE INDEX IF NOT EXISTS idx_pg_products_updated_at ON products(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pg_products_total_stock ON products(current_total_stock DESC);
CREATE INDEX IF NOT EXISTS idx_scraper_runs_started_at ON scraper_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_scraper_errors_run_id ON scraper_errors(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scraper_events_run_created ON scraper_events(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scraper_events_section_created ON scraper_events(section, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scraper_brand_progress_run_status ON scraper_brand_progress(run_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pg_products_kpi         ON products(is_in_stock, selling_price, mrp, discount_percentage);
CREATE INDEX IF NOT EXISTS idx_pg_products_cto_scope_core
    ON products(category, gender, is_myntra_label, selling_price)
    INCLUDE (brand, mrp, discount_percentage, is_in_stock, average_rating, total_ratings_count, total_reviews_count, sub_category);
CREATE INDEX IF NOT EXISTS idx_pg_products_subcat_lower ON products((LOWER(COALESCE(sub_category, ''))));
CREATE INDEX IF NOT EXISTS idx_pg_products_color_lower  ON products((LOWER(COALESCE(primary_color, ''))));
CREATE INDEX IF NOT EXISTS idx_pg_products_title_trgm   ON products USING GIN (LOWER(COALESCE(title, '')) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_pg_products_brand_trgm   ON products USING GIN (LOWER(COALESCE(brand, '')) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_pg_products_fabric_trgm  ON products USING GIN (LOWER(COALESCE(fabric, '')) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_pg_products_fit_trgm     ON products USING GIN (LOWER(COALESCE(fit, '')) gin_trgm_ops);

-- product_sizes
CREATE INDEX IF NOT EXISTS idx_pg_sizes_prod           ON product_sizes(product_id);
CREATE INDEX IF NOT EXISTS idx_pg_sizes_size           ON product_sizes(size);
CREATE INDEX IF NOT EXISTS idx_pg_sizes_prod_avail     ON product_sizes(product_id, available);
CREATE INDEX IF NOT EXISTS idx_pg_sizes_avail_size_inv ON product_sizes(available, size, inventory_count);

-- analytics tables
CREATE INDEX IF NOT EXISTS idx_pg_snapshots_date       ON daily_inventory_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_pg_snapshots_brand      ON daily_inventory_snapshots(brand);
CREATE INDEX IF NOT EXISTS idx_pg_snapshots_prod_date  ON daily_inventory_snapshots(product_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_pg_snapshots_brand_date ON daily_inventory_snapshots(brand, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_pg_sales_date           ON daily_sales_analytics(analytics_date);
CREATE INDEX IF NOT EXISTS idx_pg_sales_brand          ON daily_sales_analytics(brand);
CREATE INDEX IF NOT EXISTS idx_pg_sales_prod_date      ON daily_sales_analytics(product_id, analytics_date);
CREATE INDEX IF NOT EXISTS idx_pg_sales_brand_date     ON daily_sales_analytics(brand, analytics_date);
CREATE INDEX IF NOT EXISTS idx_pg_sales_date_product_cover
    ON daily_sales_analytics(analytics_date, product_id)
    INCLUDE (brand, category, units_sold, revenue_generated, stock_added, ros, stock_status);
CREATE INDEX IF NOT EXISTS idx_pg_sales_velocity_cover
    ON daily_sales_analytics(units_sold DESC, revenue_generated DESC)
    INCLUDE (analytics_date, product_id, brand, category, stock_added, ros, stock_status);
"""


def create_schema():
    print("Connecting to PostgreSQL…")
    conn = psycopg2.connect(**PG_DSN)
    conn.autocommit = True
    cur = conn.cursor()

    print("Creating tables and indexes…")
    for stmt in DDL.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                cur.execute(stmt)
            except Exception as e:
                print(f"  WARN: {e}")

    cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")
    n = cur.fetchone()[0]
    print(f"Schema ready — {n} tables in public schema.")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    create_schema()
