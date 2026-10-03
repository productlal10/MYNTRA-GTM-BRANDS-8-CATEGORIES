#!/usr/bin/env python3
"""
LAL10 Fashion Intelligence — Central Hub Server
Serves the unified category hub on port 3000 with Central Authentication SSO.
"""

import os
import sys
import json
import re
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
ENV_FILE = BASE_DIR / ".env"


def _env_setting(key: str, default: str = "") -> str:
    """Read a setting from the environment, falling back to the git-ignored .env file.

    The file is re-read on each call so secrets can be added without restarting the hub.
    """
    if os.environ.get(key):
        return os.environ[key].strip()
    try:
        for line in ENV_FILE.read_text("utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return default


def _signing_secret() -> bytes:
    secret = _env_setting("SECRET_KEY")
    if not secret:
        # No configured secret: use a random per-process one so tokens can never be forged
        # from a value published in source control (sessions then last one process lifetime).
        import secrets as _secrets
        print("WARNING: SECRET_KEY is not set in .env; using a random per-process signing key.")
        return _secrets.token_bytes(32)
    return secret.encode("utf-8")


ADMIN_EMAIL = _env_setting("ADMIN_EMAIL", "fashionos@lal10.com").strip().lower()
ADMIN_PASSWORD = _env_setting("ADMIN_PASSWORD").strip()
SECRET_KEY = _signing_secret()
ACTIVE_SESSIONS = set()

def _load_valid_emails():
    configured = _env_setting("VALID_EMAILS", "")
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
    # Always re-check signature, expiry and email: caching a token as valid would let it
    # outlive its 7 days, or a user removed from VALID_EMAILS, for the life of the process.
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
                if not email or email not in VALID_EMAILS:
                    return False
                expected = hmac.new(SECRET_KEY, f"{email}:{token_ts}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
                if hmac.compare_digest(parts[3], expected):
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
# Defined once in categories.json (see categories.py), in pipeline order.
from categories import load_categories
SERVICES = load_categories()

HUB_PORT = int(os.environ.get("HUB_PORT", 3000))


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
            # These require login, so shared caches must not store them.
            resp.headers["Cache-Control"] = "private, max-age=5, stale-while-revalidate=15"

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
            "/api/auth/forgot-password", "/api/auth/social-login",
            "/api/sidebar-config",
        }
        if path in public_paths or path.endswith((".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2")):
            return None

        token = get_current_token()
        if is_valid_token(token):
            return None

        if path.startswith("/api/"):
            return jsonify({"detail": "Authentication required."}), 401
        return redirect("/login")

    # Scraping and syncing run on the local machine only. On EC2 every visit arrives through
    # nginx, which adds X-Forwarded-For, so the deployed site gets these controls disabled.
    _LOCAL_ONLY_CONTROLS = {
        "/api/alan/scrape/start", "/api/alan/scrape/stop",
        "/api/alan/sync/ec2", "/api/alan/sync/ec2/stop",
    }

    def _scraper_controls_allowed() -> bool:
        return (
            request.remote_addr in ("127.0.0.1", "::1")
            and not request.headers.get("X-Forwarded-For")
            and request.host.split(":")[0] in ("localhost", "127.0.0.1", "[::1]")
        )

    @app.before_request
    def enforce_local_only_controls():
        changes_sources = request.path.startswith("/api/alan/sources/") and request.method == "POST"
        if (request.path in _LOCAL_ONLY_CONTROLS or changes_sources) and not _scraper_controls_allowed():
            return jsonify({"error": "Scraper and sync controls are only available on localhost."}), 403
        return None

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

    @app.route("/assets/<path:filename>")
    def hub_assets(filename):
        return send_from_directory(str(HUB_DIR / "assets"), filename)

    # The login page offers these; without routes they fell into the auth wall (401) and the
    # page reported "Recovery instructions dispatched" although nothing happened.
    @app.route("/api/auth/forgot-password", methods=["POST"])
    def auth_forgot_password():
        return jsonify({"message": "Password resets are handled by your workspace administrator — please contact them."}), 200

    @app.route("/api/auth/social-login", methods=["POST"])
    def auth_social_login():
        # No identity provider is wired up, so never issue a session from here.
        return jsonify({"detail": "Single sign-on is not enabled for this workspace. Please sign in with your work email and password."}), 503

    @app.route("/api/auth/login", methods=["POST"])
    def auth_login():
        data = request.get_json(silent=True) or {}
        email = data.get("email", "").strip().lower()
        password = data.get("password", "").strip()

        if not email or not password:
            return jsonify({"detail": "Email and password are required."}), 400

        if not ADMIN_PASSWORD or email not in VALID_EMAILS or not hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
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
            # HTTPS arrives through nginx, which sets X-Forwarded-Proto; never send the token over plain HTTP then.
            secure=request.is_secure or request.headers.get("X-Forwarded-Proto", "") == "https",
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
        # Card details live in categories.json ("hub_card"); slugs match /api/services.
        cards = sorted(SERVICES, key=lambda s: s["hub_card"]["order"])
        categories = [
            {"slug": s["slug"], "port": s["port"], **{k: v for k, v in s["hub_card"].items() if k != "order"}}
            for s in cards
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
                # The cache is shared by every visitor; whether controls are allowed is per request.
                return jsonify({**_STATUS_CACHE["data"], "scraper_controls": _scraper_controls_allowed()})

            pg_host = _env_setting("PG_HOST", "127.0.0.1")
            pg_port = int(_env_setting("PG_PORT", "5432"))
            pg_user = _env_setting("PG_USER", "postgres")
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
                    "admin_title": svc.get("admin_title", svc["name"]),
                    "subtitle": svc.get("subtitle", ""),
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
                "ec2_host": _env_setting("EC2_HOST"),
                "timestamp": now
            }
            _STATUS_CACHE["timestamp"] = now
            _STATUS_CACHE["data"] = result
            return jsonify({**result, "scraper_controls": _scraper_controls_allowed()})

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

    # ── Extra sources: Myntra / Shopify URLs scraped with a category (see source_scraper.py) ──
    _SOURCE_JOBS = {}
    _MAX_SOURCES = 200

    def _sources_file(svc):
        return BASE_DIR / svc["folder"] / "sources.json"

    def _read_sources(svc):
        try:
            return json.loads(_sources_file(svc).read_text("utf-8")).get("urls", [])
        except (OSError, ValueError):
            return []

    def _write_sources(svc, urls):
        path = _sources_file(svc)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"urls": urls}, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)

    def _sources_log(svc):
        return BASE_DIR / ".scraper_logs" / f"{svc['folder'].replace(' ', '_')}_sources.log"

    def _source_job_running(folder):
        proc = _SOURCE_JOBS.get(folder)
        return bool(proc and proc.poll() is None)

    @app.route("/api/alan/sources/<path:folder>", methods=["GET"])
    def api_alan_sources_list(folder):
        svc = _match_service(folder)
        if not svc:
            return jsonify({"error": "Unknown category folder"}), 400
        from source_scraper import classify
        urls = [{**u, "kind": classify(u["url"])[0]} for u in _read_sources(svc)]
        return jsonify({"folder": svc["folder"], "name": svc["name"], "urls": urls,
                        "running": _source_job_running(svc["folder"])})

    @app.route("/api/alan/sources/<path:folder>", methods=["POST"])
    def api_alan_sources_add(folder):
        svc = _match_service(folder)
        if not svc:
            return jsonify({"error": "Unknown category folder"}), 400
        from source_scraper import classify
        raw = str((request.get_json(silent=True) or {}).get("urls") or "")
        current = _read_sources(svc)
        known = {u["url"] for u in current}
        added, rejected = [], []
        for line in re.split(r"[\s,]+", raw):
            url = line.strip()
            if not url:
                continue
            kind, detail = classify(url)
            if kind == "invalid":
                rejected.append({"url": url, "reason": detail})
            elif url not in known:
                known.add(url)
                added.append({"url": url, "added_at": time.strftime("%Y-%m-%d %H:%M")})
        if len(current) + len(added) > _MAX_SOURCES:
            return jsonify({"error": f"At most {_MAX_SOURCES} URLs per category"}), 400
        _write_sources(svc, current + added)
        return jsonify({"added": len(added), "rejected": rejected, "total": len(current) + len(added)})

    @app.route("/api/alan/sources/<path:folder>/remove", methods=["POST"])
    def api_alan_sources_remove(folder):
        svc = _match_service(folder)
        if not svc:
            return jsonify({"error": "Unknown category folder"}), 400
        url = str((request.get_json(silent=True) or {}).get("url") or "")
        current = _read_sources(svc)
        kept = [u for u in current if u["url"] != url]
        _write_sources(svc, kept)
        return jsonify({"removed": len(current) - len(kept), "total": len(kept)})

    @app.route("/api/alan/sources/<path:folder>/scrape", methods=["POST"])
    def api_alan_sources_scrape(folder):
        svc = _match_service(folder)
        if not svc:
            return jsonify({"error": "Unknown category folder"}), 400
        if not _read_sources(svc):
            return jsonify({"error": "No URLs saved for this category yet"}), 400
        # One writer per database: not while this category's scraper or a sources job runs.
        if _running_scrapers().get(svc["folder"]) or _source_job_running(svc["folder"]):
            return jsonify({"error": f"{svc['folder']} is already being scraped — try again when it finishes"}), 409
        log_path = _sources_log(svc)
        log_path.parent.mkdir(exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8")
        handle.write(f"\n===== Sources scrape started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        handle.flush()
        # --snapshot so the new products show in snapshot-based views straight away.
        proc = subprocess.Popen([SCRAPER_PYTHON, str(BASE_DIR / "source_scraper.py"), "--snapshot"],
                                cwd=str(BASE_DIR / svc["folder"]), stdout=handle, stderr=subprocess.STDOUT,
                                start_new_session=True)
        _reap(proc, handle)
        _SOURCE_JOBS[svc["folder"]] = proc
        return jsonify({"status": "started", "folder": svc["folder"], "pid": proc.pid})

    @app.route("/api/alan/sources/<path:folder>/log", methods=["GET"])
    def api_alan_sources_log(folder):
        svc = _match_service(folder)
        if not svc:
            return jsonify({"error": "Unknown category folder"}), 400
        lines = []
        try:
            with open(_sources_log(svc), "r", encoding="utf-8", errors="replace") as f:
                lines = [l.rstrip("\n") for l in f.readlines()[-150:]]
        except OSError:
            pass
        return jsonify({"lines": lines, "running": _source_job_running(svc["folder"])})

    _SYNC_STATE = {
        "running": False,
        "pid": None,
        "proc": None,
        "folder": None,
        "start_time": 0,
        "end_time": 0,
        "exit_code": None,
        "log_file": None,
    }
    _SYNC_LOCK = threading.Lock()

    @app.route("/api/alan/sync/ec2", methods=["POST"])
    def api_alan_sync_ec2():
        """Trigger sync of databases and code to EC2."""
        data = request.get_json(silent=True) or {}
        target_folder = data.get("folder", "all")
        if target_folder != "all":
            # The name reaches pipeline.sh and the log file path, so only known categories pass.
            matched_svc = _match_service(target_folder)
            if not matched_svc:
                return jsonify({"error": f"Unknown category folder '{target_folder}'"}), 400
            target_folder = matched_svc["folder"]

        with _SYNC_LOCK:
            if _SYNC_STATE["running"]:
                proc = _SYNC_STATE.get("proc")
                if proc and proc.poll() is None:
                    return jsonify({
                        "error": "EC2 Sync is already in progress",
                        "folder": _SYNC_STATE["folder"],
                        "pid": _SYNC_STATE["pid"],
                        "elapsed": round(time.time() - _SYNC_STATE["start_time"], 1)
                    }), 409
                else:
                    _SYNC_STATE["running"] = False

            log_dir = BASE_DIR / ".pipeline_logs"
            log_dir.mkdir(exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            log_file = log_dir / f"sync_ec2_{target_folder.replace(' ', '_')}_{ts}.log"

            cmd = ["bash", str(BASE_DIR / "pipeline.sh"), "--sync-only"]
            if target_folder != "all":
                cmd.extend(["--only", target_folder])

            log_handle = open(log_file, "w", encoding="utf-8")
            proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True)
            _reap(proc, log_handle)

            _SYNC_STATE["running"] = True
            _SYNC_STATE["pid"] = proc.pid
            _SYNC_STATE["proc"] = proc
            _SYNC_STATE["folder"] = target_folder
            _SYNC_STATE["start_time"] = time.time()
            _SYNC_STATE["end_time"] = 0
            _SYNC_STATE["exit_code"] = None
            _SYNC_STATE["log_file"] = str(log_file)

            return jsonify({
                "status": "started",
                "folder": target_folder,
                "pid": proc.pid,
                "log_file": str(log_file)
            })

    @app.route("/api/alan/sync/ec2/status")
    def api_alan_sync_ec2_status():
        """Retrieve live status and output logs of EC2 Sync."""
        with _SYNC_LOCK:
            proc = _SYNC_STATE.get("proc")
            running = _SYNC_STATE["running"]
            if running and proc:
                exit_code = proc.poll()
                if exit_code is not None:
                    _SYNC_STATE["running"] = False
                    _SYNC_STATE["exit_code"] = exit_code
                    _SYNC_STATE["end_time"] = time.time()
                    running = False

            log_file = _SYNC_STATE.get("log_file")
            lines = []
            if log_file and os.path.exists(log_file):
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        lines = [l.rstrip("\r\n") for l in f.readlines()[-200:]]
                except Exception:
                    pass

            start_t = _SYNC_STATE["start_time"]
            elapsed = round((time.time() - start_t) if running else (_SYNC_STATE["end_time"] - start_t if start_t else 0), 1)

            return jsonify({
                "status": "success",
                "running": running,
                "folder": _SYNC_STATE.get("folder"),
                "pid": _SYNC_STATE.get("pid"),
                "elapsed": max(0, elapsed),
                "exit_code": _SYNC_STATE.get("exit_code"),
                "lines": lines
            })

    # ─── SIDEBAR & NAVIGATION CONFIGURATION ───────────────────────
    SIDEBAR_CONFIG_FILE = BASE_DIR / "sidebar_config.json"

    def _default_sidebar_config():
        return {
            "_comment": "Sidebar and navigation management configuration. Managed via /alan Admin Console.",
            "global": {
                "sidebar_enabled": True,
                "default_collapsed": False,
                "allow_user_toggle": True,
                "show_brand_header": True,
                "show_sync_status": True,
                "show_user_profile": True,
                "show_sub_sidebars": True,
                "default_landing_view": "dashboard",
                "items": {
                    "dashboard": {"enabled": True, "label": "Dashboard", "order": 1, "category": "Overview"},
                    "demand-radar": {"enabled": True, "label": "Demand Radar", "order": 2, "category": "Intelligence"},
                    "demand-radar-beta": {"enabled": True, "label": "Demand Radar (Beta)", "order": 3, "category": "Intelligence"},
                    "comparator": {"enabled": True, "label": "Brand Comparator", "order": 3, "category": "Intelligence"},
                    "price-intel": {"enabled": True, "label": "Price Intelligence", "order": 4, "category": "Intelligence"},
                    "revenue-intel": {"enabled": True, "label": "Revenue Intelligence", "order": 5, "category": "Intelligence"},
                    "colors": {"enabled": True, "label": "Color Intelligence", "order": 6, "category": "Intelligence"},
                    "fabric": {"enabled": True, "label": "Fabric Intelligence", "order": 7, "category": "Intelligence"},
                    "catalog": {"enabled": True, "label": "Product Catalog", "order": 8, "category": "Catalog"},
                    "brands": {"enabled": True, "label": "Brands Intelligence", "order": 9, "category": "Intelligence"},
                    "compare": {"enabled": True, "label": "Compare Products", "order": 10, "category": "Catalog"},
                    "category-analysis": {"enabled": True, "label": "Category Analysis", "order": 11, "category": "Intelligence"},
                    "insights": {"enabled": True, "label": "Trend Intel", "order": 12, "category": "Analytics"},
                    "daily-movement": {"enabled": True, "label": "Market Moves", "order": 13, "category": "Analytics"},
                    "analytics-intelligence": {"enabled": True, "label": "Sales Monitor", "order": 14, "category": "Analytics"},
                    "scraper-logs": {"enabled": True, "label": "Logs", "order": 15, "category": "System"},
                    "data-issues": {"enabled": True, "label": "Data Issues", "order": 16, "category": "System"},
                    "ai-assistant": {"enabled": True, "label": "AI Assistant", "order": 17, "category": "System"}
                }
            },
            "categories": {}
        }

    def _load_sidebar_config():
        try:
            if SIDEBAR_CONFIG_FILE.exists():
                return json.loads(SIDEBAR_CONFIG_FILE.read_text("utf-8"))
        except Exception as e:
            print(f"[WARN] Failed to read sidebar_config.json: {e}")
        return _default_sidebar_config()

    def _save_sidebar_config(data):
        try:
            SIDEBAR_CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            print(f"[ERROR] Failed to write sidebar_config.json: {e}")
            return False

    @app.route("/api/alan/sidebar-config", methods=["GET"])
    def api_alan_sidebar_config_get():
        cfg = _load_sidebar_config()
        # Include list of available categories
        return jsonify({
            "config": cfg,
            "categories": [{"folder": s["folder"], "name": s["name"], "admin_title": s.get("admin_title", s["name"])} for s in SERVICES]
        })

    @app.route("/api/alan/sidebar-config", methods=["POST"])
    def api_alan_sidebar_config_post():
        data = request.get_json(silent=True) or {}
        config_payload = data.get("config", data)
        if not isinstance(config_payload, dict):
            return jsonify({"error": "Invalid configuration format"}), 400

        # Preserve root structure if needed
        existing = _load_sidebar_config()
        if "global" in config_payload:
            existing["global"] = config_payload["global"]
        if "categories" in config_payload:
            existing["categories"] = config_payload["categories"]

        if _save_sidebar_config(existing):
            return jsonify({"status": "saved", "config": existing})
        else:
            return jsonify({"error": "Failed to write sidebar configuration"}), 500

    @app.route("/api/alan/sidebar-config/reset", methods=["POST"])
    def api_alan_sidebar_config_reset():
        defaults = _default_sidebar_config()
        if _save_sidebar_config(defaults):
            return jsonify({"status": "reset", "config": defaults})
        return jsonify({"error": "Failed to reset sidebar configuration"}), 500

    @app.route("/api/sidebar-config", methods=["GET"])
    def api_sidebar_config_resolved():
        target_folder = request.args.get("folder") or request.args.get("category") or ""
        cfg = _load_sidebar_config()
        global_cfg = cfg.get("global", {})
        if target_folder and target_folder in cfg.get("categories", {}):
            cat_cfg = cfg["categories"][target_folder]
            # Deep merge global and category settings
            merged = json.loads(json.dumps(global_cfg))
            for k, v in cat_cfg.items():
                if k == "items" and isinstance(v, dict):
                    merged.setdefault("items", {})
                    for item_k, item_v in v.items():
                        merged["items"][item_k] = {**merged["items"].get(item_k, {}), **item_v}
                else:
                    merged[k] = v
            return jsonify(merged)
        return jsonify(global_cfg)



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
