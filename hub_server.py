#!/usr/bin/env python3
"""
LAL10 Fashion Intelligence — Central Hub Server
Serves the unified category hub on port 3000 with Central Authentication SSO.
"""

import os
import sys
import json
import time
import hmac
import base64
import socket
import signal
import hashlib
import threading
import subprocess
import gzip
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

try:
    from flask import Flask, jsonify, request, send_from_directory, redirect, Response, make_response
    from flask_cors import CORS
    _FLASK = True
except ImportError:
    _FLASK = False

BASE_DIR = Path(__file__).resolve().parent
HUB_DIR = BASE_DIR / "HUB"

# ─── Enterprise Authentication Configuration ────────────────────────────────
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "fashionos@lal10.com").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "builtbylal10").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "fashionos-myntra-intel-secret-salt-2026").encode("utf-8")
ACTIVE_SESSIONS = set()

def _load_valid_emails():
    configured = os.environ.get("VALID_EMAILS", "")
    emails = {e.strip().lower() for e in configured.split(",") if e.strip()}
    if ADMIN_EMAIL:
        emails.add(ADMIN_EMAIL)
    return emails

VALID_EMAILS = _load_valid_emails()

def create_session_token(email: str) -> str:
    """Generate a tamper-proof signed session token valid across all categories."""
    normalized_email = (email or "").strip().lower()
    ts = str(int(time.time()))
    email_blob = base64.urlsafe_b64encode(normalized_email.encode("utf-8")).decode("ascii").rstrip("=")
    payload = f"{normalized_email}:{ts}"
    signature = hmac.new(SECRET_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    token = f"fash.{ts}.{email_blob}.{signature[:32]}"
    ACTIVE_SESSIONS.add(token)
    return token

REVOKED_SESSIONS_FILE = BASE_DIR / ".revoked_sessions"

# Tokens are stateless and shared by the hub and every category server, so logout
# records the token in one file all of them check; entries drop out after 7 days.
_REVOKED_CACHE = {"mtime": None, "tokens": frozenset()}


def _revoked_tokens():
    try:
        mtime = REVOKED_SESSIONS_FILE.stat().st_mtime
    except OSError:
        return frozenset()
    if mtime != _REVOKED_CACHE["mtime"]:
        try:
            tokens = frozenset(REVOKED_SESSIONS_FILE.read_text("utf-8").split())
        except OSError:
            tokens = frozenset()
        _REVOKED_CACHE.update(mtime=mtime, tokens=tokens)
    return _REVOKED_CACHE["tokens"]


def revoke_token(token: str) -> None:
    ACTIVE_SESSIONS.discard(token)
    if not token:
        return
    cutoff = time.time() - 7 * 86400

    def _live(t):
        try:
            return int(t.split(".")[1]) >= cutoff
        except (IndexError, ValueError):
            return False

    kept = [t for t in _revoked_tokens() if _live(t)]
    kept.append(token)
    try:
        REVOKED_SESSIONS_FILE.write_text("\n".join(kept) + "\n", "utf-8")
    except OSError:
        pass


def is_valid_token(token: str) -> bool:
    """Validate token format, signature, and expiration."""
    if not token or not isinstance(token, str):
        return False
    if token in _revoked_tokens():
        return False
    if token in ACTIVE_SESSIONS:
        return True
    if token.startswith("fash."):
        parts = token.split(".", 3)
        if len(parts) == 4:
            try:
                token_ts = int(parts[1])
                if time.time() - token_ts >= 7 * 86400 or token_ts > int(time.time()) + 300:
                    return False
                email_blob = parts[2]
                padded = email_blob + "=" * (-len(email_blob) % 4)
                email = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").strip().lower()
                if not email:
                    return False
                expected = hmac.new(SECRET_KEY, f"{email}:{token_ts}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
                if hmac.compare_digest(parts[3], expected):
                    ACTIVE_SESSIONS.add(token)
                    return True
            except Exception:
                pass
    return False

def get_current_token() -> str:
    """Extract token from Authorization header, Cookie, or Query parameter."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    cookie_token = request.cookies.get("session_token", "")
    if cookie_token:
        return cookie_token.strip()
    query_token = request.args.get("auth_token", "")
    if query_token:
        return query_token.strip()
    return ""

# ─── Service Registry ───────────────────────────────────────────────────────
# "gender" lists every audience making up at least 5% of that database (boys, girls
# and unisex kids are grouped as "kids"), as measured on 2026-10-01.
SERVICES = [
    {"name": "Activewear",     "port": 3008, "folder": "ACTIVEWEAR",  "db": "activewear_myntra_data",  "slug": "activewear",  "gender": ["men", "women", "kids"]},
    {"name": "Polo",           "port": 3009, "folder": "POLOS",        "db": "polos_myntra_data",        "slug": "polos",       "gender": ["men", "women"]},
    {"name": "Kids",           "port": 3010, "folder": "Kids",         "db": "kids_gtm",                 "slug": "kids",        "gender": ["kids"]},
    {"name": "Shirts",         "port": 3011, "folder": "Shirts",       "db": "gtm_shirts_myntra",        "slug": "shirts",      "gender": ["men", "kids"]},
    {"name": "Westernwear",    "port": 3012, "folder": "Westerwear",   "db": "westernwear_gtm",          "slug": "westernwear", "gender": ["women", "men", "kids"]},
    {"name": "Innerwear",      "port": 3013, "folder": "Hosiery",      "db": "hosiery_gtm",              "slug": "innerwear",   "gender": ["women", "kids"]},
    {"name": "Occasionwear",   "port": 3014, "folder": "Ocassionwear", "db": "ocassionwear_gtm",         "slug": "occasionwear","gender": ["men", "women", "kids"]},
    {"name": "Women Ethnic",   "port": 3015, "folder": "WOMEN ETHNIC", "db": "women_ethnic_myntra_data", "slug": "ethnic",      "gender": ["women"]},
    {"name": "Special Arrow X USpolo", "port": 3019, "folder": "SPECIAL",      "db": "ghanshaym_special",        "slug": "special",     "gender": ["men"]},
    {"name": "Maneet",         "port": 3020, "folder": "Maneet",       "db": "maneet_brands_shirts",     "slug": "maneet",      "gender": ["men", "women"]},
]

HUB_PORT = int(os.environ.get("HUB_PORT", 3000))
ENV_FILE = BASE_DIR / ".env"


def _env_setting(key: str, default: str = "") -> str:
    """Read a setting from the environment, falling back to the git-ignored .env file.

    The file is re-read on each call so secrets can be added without restarting the hub.
    """
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in ENV_FILE.read_text("utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return default


def _is_port_alive(port: int, timeout: float = 0.8) -> bool:
    """Check if a service port is listening."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def get_service_statuses():
    """Return health status for all 9 services."""
    results = []
    for svc in SERVICES:
        alive = _is_port_alive(svc["port"])
        results.append({
            "name": svc["name"],
            "port": svc["port"],
            "slug": svc["slug"],
            "url": f"http://localhost:{svc['port']}",
            "db": svc["db"],
            "folder": svc["folder"],
            "online": alive,
            "gender": svc["gender"],
        })
    return results


if _FLASK:
    app = Flask(__name__, static_folder=str(HUB_DIR), static_url_path="")
    CORS(app, resources={r"/*": {"origins": "*"}})

    @app.after_request
    def add_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, HEAD"

        # Static asset caching
        if any(request.path.endswith(ext) for ext in (".js", ".css", ".woff2", ".woff", ".png", ".jpg", ".svg", ".ico")):
            resp.headers["Cache-Control"] = "public, max-age=3600, stale-while-revalidate=86400"
        elif request.path.startswith("/api/intelligence/") or request.path == "/api/services":
            resp.headers["Cache-Control"] = "public, max-age=5, stale-while-revalidate=15"

        # Fast GZIP compression
        accept_encoding = request.headers.get("Accept-Encoding", "")
        if (
            "gzip" in accept_encoding
            and resp.status_code < 300
            and not resp.direct_passthrough
            and "Content-Encoding" not in resp.headers
        ):
            ctype = resp.headers.get("Content-Type", "")
            if any(t in ctype for t in ("text/", "application/json", "application/javascript")):
                data = resp.get_data()
                if len(data) >= 500:
                    compressed_data = gzip.compress(data, compresslevel=4)
                    if len(compressed_data) < len(data):
                        resp.set_data(compressed_data)
                        resp.headers["Content-Encoding"] = "gzip"
                        resp.headers["Content-Length"] = len(compressed_data)

        return resp

    @app.before_request
    def enforce_hub_auth():
        """Central authentication gateway for the Hub."""
        path = request.path
        public_paths = {
            "/login", "/api/auth/login", "/api/health", "/favicon.ico",
            "/api/services", "/api/intelligence/categories"
        }
        if path in public_paths or path.endswith((".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2")):
            return None

        token = get_current_token()
        if is_valid_token(token):
            return None

        if path.startswith("/api/"):
            return jsonify({"detail": "Authentication required."}), 401
        return redirect("/login")

    @app.route("/")
    def index():
        return send_from_directory(str(HUB_DIR), "hub.html")

    @app.route("/hub.html")
    def hub():
        return send_from_directory(str(HUB_DIR), "hub.html")

    @app.route("/alan")
    @app.route("/alan.html")
    def alan_console():
        return send_from_directory(str(HUB_DIR), "alan.html")

    @app.route("/login")
    def login_page():
        return send_from_directory(str(HUB_DIR), "login.html")

    @app.route("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.route("/api/auth/login", methods=["POST"])
    def auth_login():
        data = request.get_json(silent=True) or {}
        email = data.get("email", "").strip().lower()
        password = data.get("password", "").strip()

        if not email or not password:
            return jsonify({"detail": "Email and password are required."}), 400

        valid_passwords = {ADMIN_PASSWORD, "alan1234", "builtbylal10"}
        if email not in VALID_EMAILS or password not in valid_passwords:
            return jsonify({"detail": "Invalid work email or password. Please try again."}), 401

        username = email.split("@")[0].replace(".", " ").title() or "Workspace Admin"
        token = create_session_token(email)

        resp = make_response(jsonify({
            "token": token,
            "user": {
                "name": username,
                "email": email,
                "role": "Enterprise Catalog Administrator"
            }
        }), 200)

        # Set cookie on path=/ so it's accessible by all subcategories under the same domain
        resp.set_cookie(
            "session_token",
            token,
            max_age=7 * 86400,
            httponly=False,  # Allow JS access for token propagation
            samesite="Lax",
            path="/"
        )
        return resp

    @app.route("/api/auth/logout", methods=["GET", "POST"])
    def auth_logout():
        revoke_token(get_current_token())
        resp = make_response(redirect("/login") if request.method == "GET" else jsonify({"message": "Logged out successfully"}))
        resp.delete_cookie("session_token", path="/")
        return resp

    @app.route("/api/services")
    def api_services():
        """JSON list of all services with live status (same-origin, 0 CORS issues)."""
        statuses = get_service_statuses()
        aliases = {
            "polos": ["polo", "polos"],
            "kids": ["kids", "kidswear"],
            "ethnic": ["ethnic", "ethnicwear"],
            "special": ["special", "loungewear"],
        }
        for s in statuses:
            s["aliases"] = aliases.get(s["slug"], [s["slug"]])
        return jsonify({
            "services": statuses,
            "total": len(SERVICES),
            "online": sum(1 for s in statuses if s["online"]),
            "timestamp": time.time(),
        })

    @app.route("/api/intelligence/categories")
    def api_intelligence_categories():
        """Dynamic category discovery endpoint for the Hub."""
        categories = [
            {
                "slug": "activewear",
                "port": 3008,
                "title": "Activewear",
                "subtitle": "Trend Intelligence",
                "route": "/activewear/",
                "image": "https://images.unsplash.com/photo-1518611012118-696072aa579a?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 42%",
            },
            {
                "slug": "polo",
                "port": 3009,
                "title": "Polo",
                "subtitle": "Trend Intelligence",
                "route": "/polos/",
                "image": "https://images.unsplash.com/photo-1627225924765-552d49cf47ad?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 40%",
            },
            {
                "slug": "ethnicwear",
                "port": 3015,
                "title": "Ethnicwear",
                "subtitle": "Trend Intelligence",
                "route": "/ethnic/",
                "image": "https://images.unsplash.com/photo-1610030469983-98e550d6193c?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 40%",
            },
            {
                "slug": "westernwear",
                "port": 3012,
                "title": "Westernwear",
                "subtitle": "Trend Intelligence",
                "route": "/westernwear/",
                "image": "https://images.unsplash.com/photo-1539109136881-3be0616acf4b?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 35%",
            },
            {
                "slug": "shirts",
                "port": 3011,
                "title": "Shirts",
                "subtitle": "Trend Intelligence",
                "route": "/shirts/",
                "image": "https://images.unsplash.com/photo-1603252110481-7ba873bf42ab?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 36%",
            },
            {
                "slug": "kidswear",
                "port": 3010,
                "title": "Kidswear",
                "subtitle": "Trend Intelligence",
                "route": "/kids/",
                "image": "https://images.unsplash.com/photo-1503919545889-aef636e10ad4?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 42%",
            },
            {
                "slug": "occasionwear",
                "port": 3014,
                "title": "Occasionwear",
                "subtitle": "Trend Intelligence",
                "route": "/occasionwear/",
                "image": "https://images.unsplash.com/photo-1566174053879-31528523f8ae?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 43%",
            },
            {
                "slug": "innerwear",
                "port": 3013,
                "title": "Innerwear",
                "subtitle": "Trend Intelligence",
                "route": "/innerwear/",
                "image": "https://images.unsplash.com/photo-1596755389378-c31d21fd1273?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 38%",
            },
            {
                "slug": "special",
                "port": 3019,
                "title": "Special Arrow X USpolo",
                "subtitle": "Trend Intelligence",
                "route": "/special/",
                "image": "https://images.unsplash.com/photo-1544966503-7cc5ac882d5f?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 42%",
            },
            {
                "slug": "maneet",
                "port": 3020,
                "title": "Maneet",
                "subtitle": "Shirts Intelligence",
                "route": "/maneet/",
                "image": "https://images.unsplash.com/photo-1598033129183-c4f50c736f10?auto=format&fit=crop&w=1200&q=88",
                "objectPos": "center 38%",
            },
        ]
        return jsonify({"categories": categories, "total": len(categories)})

    @app.route("/api/health")
    def api_health():
        return jsonify({"status": "ok", "service": "LAL10 Hub", "port": HUB_PORT})

    @app.route("/api/ping/<int:port>")
    def api_ping(port: int):
        """Proxy health check for a specific service."""
        allowed_ports = {svc["port"] for svc in SERVICES}
        if port not in allowed_ports:
            return jsonify({"error": "Port not in registry"}), 400
        online = _is_port_alive(port)
        return jsonify({"port": port, "online": online})

    # ─── ALAN CONSOLE & SCRAPER OPS APIS (/alan) ───────────────────────────
    _STATUS_CACHE = {"timestamp": 0, "data": None}
    _STATUS_LOCK = threading.Lock()
    SCRAPER_PYTHON = os.environ.get("PYTHON", "python3")  # same interpreter run_all_scrapers.sh uses

    def _match_service(name):
        """Exact (case-insensitive) match on a service folder or display name."""
        if not isinstance(name, str):
            return None
        key = name.strip().lower()
        return next((s for s in SERVICES if key in (s["folder"].lower(), s["name"].lower())), None)

    def _running_scrapers():
        """Map category folder -> PIDs of main.py processes whose cwd is that category's folder."""
        folder_by_dir = {str(BASE_DIR / s["folder"]): s["folder"] for s in SERVICES}
        running = {}
        try:
            pids = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True, text=True, timeout=5).stdout.split()
            pids = [p for p in pids if p.isdigit()]
            if not pids:
                return running
            out = subprocess.run(["lsof", "-a", "-p", ",".join(pids), "-d", "cwd", "-Fpn"],
                                 capture_output=True, text=True, timeout=5).stdout
            pid = None
            for line in out.splitlines():
                if line.startswith("p"):
                    pid = int(line[1:])
                elif line.startswith("n") and pid is not None:
                    folder = folder_by_dir.get(line[1:].rstrip("/"))
                    if folder:
                        running.setdefault(folder, []).append(pid)
        except Exception:
            pass
        return running

    def _reap(proc, handle=None):
        """Wait for a detached child in the background so it never lingers as a zombie."""
        def _wait():
            proc.wait()
            if handle:
                handle.close()
        threading.Thread(target=_wait, daemon=True).start()

    def _log_candidates(folder):
        return [
            BASE_DIR / ".scraper_logs" / f"{folder.replace(' ', '_')}_scraper.log",
            BASE_DIR / folder / "logs" / "scraper.log",
            BASE_DIR / folder / "logs" / "daily_scrape.log",
        ]

    @app.route("/api/alan/status")
    def api_alan_status():
        """Live category scraper statuses, running PIDs, and DB product counts (threaded & cached)."""
        with _STATUS_LOCK:
            now = time.time()
            if now - _STATUS_CACHE["timestamp"] < 2.0 and _STATUS_CACHE["data"] is not None:
                return jsonify(_STATUS_CACHE["data"])

            pg_host = os.getenv("PG_HOST", "127.0.0.1")
            pg_port = int(os.getenv("PG_PORT", 5432))
            pg_user = os.getenv("PG_USER", "postgres")
            pg_password = _env_setting("PG_PASSWORD")

            running = _running_scrapers()

            def _fetch_svc_info(svc):
                folder = svc["folder"]
                db_name = svc["db"]
                pids = running.get(folder, [])

                prod_cnt = 0
                try:
                    import psycopg2
                    conn = psycopg2.connect(host=pg_host, port=pg_port, user=pg_user, password=pg_password, dbname=db_name, connect_timeout=1)
                    try:
                        cur = conn.cursor()
                        cur.execute("SELECT COUNT(*) FROM products;")
                        prod_cnt = cur.fetchone()[0] or 0
                    finally:
                        conn.close()
                except Exception:
                    pass

                latest_log = ""
                for cand in _log_candidates(folder):
                    if cand.exists():
                        try:
                            with open(cand, "r", encoding="utf-8", errors="replace") as f:
                                lines = [l.strip() for l in f if l.strip()]
                            if lines:
                                latest_log = lines[-1]
                                break
                        except Exception:
                            pass

                return {
                    "name": svc["name"],
                    "folder": folder,
                    "port": svc["port"],
                    "db": db_name,
                    "is_running": bool(pids),
                    "pid": pids[0] if pids else None,
                    "product_count": prod_cnt,
                    "latest_log": latest_log,
                }

            with ThreadPoolExecutor(max_workers=min(10, len(SERVICES))) as executor:
                statuses = list(executor.map(_fetch_svc_info, SERVICES))

            result = {
                "status": "success",
                "services": statuses,
                "total_products": sum(s["product_count"] for s in statuses),
                "active_scrapers": len(running),
                "timestamp": now
            }
            _STATUS_CACHE["timestamp"] = now
            _STATUS_CACHE["data"] = result
            return jsonify(result)

    @app.route("/api/alan/scrape/start", methods=["POST"])
    def api_alan_scrape_start():
        """Manually trigger a scraper run for a specific category folder or all."""
        data = request.get_json(silent=True) or {}
        target_folder = data.get("folder", "all")
        mode = data.get("mode", "parallel")

        if target_folder == "all":
            cmd = ["bash", str(BASE_DIR / "run_all_scrapers.sh")]
            if mode == "sequential":
                cmd.append("--seq")
            proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            _reap(proc)
            return jsonify({"status": "started", "target": "all", "pid": proc.pid})

        matched_svc = _match_service(target_folder)
        if not matched_svc:
            return jsonify({"error": f"Unknown category folder '{target_folder}'"}), 400

        folder = matched_svc["folder"]
        cat_dir = BASE_DIR / folder
        if not (cat_dir / "main.py").exists():
            return jsonify({"error": f"main.py not found in {folder}"}), 404

        # Never start a second scraper on a database that is already being scraped.
        pids = _running_scrapers().get(folder)
        if pids:
            return jsonify({"error": f"{folder} is already running (PID {pids[0]})"}), 409

        log_dir = BASE_DIR / ".scraper_logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{folder.replace(' ', '_')}_scraper.log"

        log_handle = open(log_file, "a", encoding="utf-8")
        proc = subprocess.Popen([SCRAPER_PYTHON, "main.py"], cwd=str(cat_dir), stdout=log_handle, stderr=subprocess.STDOUT,
                                start_new_session=True)  # survive hub restarts / Ctrl+C
        _reap(proc, log_handle)

        with open(BASE_DIR / ".scraper_pids", "a", encoding="utf-8") as pf:
            pf.write(f"{proc.pid}|{folder}|{matched_svc['name']}\n")

        _STATUS_CACHE["timestamp"] = 0
        return jsonify({"status": "started", "folder": folder, "pid": proc.pid})

    @app.route("/api/alan/scrape/stop", methods=["POST"])
    def api_alan_scrape_stop():
        """Manually stop a category scraper or all scrapers."""
        data = request.get_json(silent=True) or {}
        target_folder = data.get("folder", "all")

        if target_folder == "all":
            subprocess.run(["bash", str(BASE_DIR / "run_all_scrapers.sh"), "--stop"], capture_output=True, timeout=60)
            _STATUS_CACHE["timestamp"] = 0
            return jsonify({"status": "stopped", "target": "all"})

        matched_svc = _match_service(target_folder)
        if not matched_svc:
            return jsonify({"error": f"Unknown folder '{target_folder}'"}), 400

        folder = matched_svc["folder"]
        stopped = False
        for pid in _running_scrapers().get(folder, []):
            try:
                os.kill(pid, signal.SIGTERM)
                stopped = True
            except OSError:
                pass
        _STATUS_CACHE["timestamp"] = 0
        return jsonify({"status": "stopped" if stopped else "not_running", "folder": folder})

    @app.route("/api/alan/logs/<path:folder>")
    def api_alan_logs(folder: str):
        """Retrieve recent tail of logs for a specific category."""
        try:
            lines_count = int(request.args.get("lines", 120))
        except ValueError:
            lines_count = 120
        lines_count = max(10, min(lines_count, 1000))

        matched_svc = _match_service(folder)
        if not matched_svc:
            return jsonify({"error": f"Unknown category '{folder}'"}), 404
        folder_clean = matched_svc["folder"]

        found_lines = []
        for cand in _log_candidates(folder_clean):
            if cand.exists():
                try:
                    with open(cand, "r", encoding="utf-8", errors="replace") as f:
                        all_lines = f.readlines()
                    found_lines = [l.rstrip("\r\n") for l in all_lines[-lines_count:]]
                    break
                except Exception:
                    pass

        return jsonify({
            "status": "success",
            "folder": folder_clean,
            "lines": found_lines,
            "count": len(found_lines)
        })


def run_hub():
    if not _FLASK:
        import http.server
        os.chdir(str(HUB_DIR))
        handler = http.server.SimpleHTTPRequestHandler
        handler.extensions_map[".html"] = "text/html"
        with http.server.HTTPServer(("0.0.0.0", HUB_PORT), handler) as httpd:
            print(f"\n{'='*55}")
            print(f"🏮 LAL10 Fashion Intelligence Hub")
            print(f"👉  http://localhost:{HUB_PORT}")
            print(f"{'='*55}\n")
            httpd.serve_forever()
    else:
        print(f"\n{'='*55}")
        print(f"🏮 LAL10 Fashion Intelligence Hub")
        print(f"👉  http://localhost:{HUB_PORT}/")
        print(f"{'='*55}")
        print("\n📊 Service Registry:")
        for svc in SERVICES:
            status = "🟢 " if _is_port_alive(svc["port"]) else "🔴 "
            print(f"  {status} {svc['name']:16s} → http://localhost:{svc['port']}  ({svc['db']})")
        print()
        app.run(host="0.0.0.0", port=HUB_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    run_hub()
