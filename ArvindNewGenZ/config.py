"""Configuration settings for the Arvind New GenZ shirts Myntra scraper."""

import os
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# Load environment variables
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env", override=True)

# Database Configurations
# PostgreSQL (Application / CRUD Data)
PG_HOST = os.getenv("PG_HOST", "127.0.0.1")
PG_PORT = int(os.getenv("PG_PORT", 5432))
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD")
if not PG_PASSWORD:
    raise RuntimeError("PG_PASSWORD is not set. Add it to this project's .env file.")
PG_DBNAME = os.getenv("PG_DBNAME", "arvind_new_genz_shirts")

# DuckDB (Analytics & Reporting)
DUCKDB_PATH = BASE_DIR / os.getenv("DUCKDB_PATH", "data/arvind_new_genz_shirts_analytics.duckdb")

# Export Files
JSONL_PATH = DATA_DIR / "products_arvind_new_genz_shirts.jsonl"
CSV_PATH = DATA_DIR / "myntra_arvind_new_genz_shirts.csv"
SIZE_CSV_PATH = DATA_DIR / "myntra_arvind_new_genz_shirts_size_inventory.csv"
JSON_PRETTY_PATH = DATA_DIR / "products_sample.json"


# Log File
LOG_FILE = LOGS_DIR / "scraper.log"

# Network & Concurrency Defaults (Steady, Zero-Loss Calibration)
DEFAULT_WORKERS = 3
REQUEST_TIMEOUT = 45  # seconds (ample headroom for DNS/network)
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0  # seconds exponential multiplier
POLITE_DELAY = 0.5  # seconds delay between listing page requests

# Proxy Configuration (Cloudflare WARP proxy on port 40000 bypasses Akamai datacenter geoblock)
# Price band (₹) this category scrapes and stores; main.py and Database.save_product read it.
PRICE_MIN = 700
PRICE_MAX = 3000

PROXY_URL = os.environ.get("PROXY_URL", "").strip()
if not PROXY_URL:
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", 40000)) == 0:
            PROXY_URL = "socks5://127.0.0.1:40000"
        sock.close()
    except Exception:
        pass

PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# Anti-Bot Headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.myntra.com/",
    "Connection": "keep-alive",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

BASE_URL = "https://www.myntra.com"
