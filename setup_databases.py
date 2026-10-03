#!/usr/bin/env python3
"""
LAL10 Fashion Intelligence — PostgreSQL Database Setup
Creates all 9 databases and initializes schemas for every category.

Usage:
  python3 setup_databases.py
  python3 setup_databases.py --check    # just check, don't create
  python3 setup_databases.py --reset    # drop and recreate (CAUTION: deletes all data)
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass
import sys
import subprocess
import argparse
from pathlib import Path

# ─── Service → DB mapping ───────────────────────────────────────────────────
# Defined once in categories.json (see categories.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from categories import load_categories
SERVICES = load_categories()

BASE_DIR = Path(__file__).resolve().parent

PG_HOST     = os.getenv("PG_HOST", "127.0.0.1")
PG_PORT     = int(os.getenv("PG_PORT", 5432))
PG_USER     = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

GREEN  = "\033[0;32m"
RED    = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"
NC     = "\033[0m"

def c(color, text): return f"{color}{text}{NC}"


def get_pg_conn(dbname="postgres"):
    try:
        import psycopg2
        return psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            user=PG_USER, password=PG_PASSWORD,
            dbname=dbname,
        )
    except ImportError:
        print(c(RED, "✗ psycopg2 not installed. Run: pip install psycopg2-binary"))
        sys.exit(1)
    except Exception as e:
        print(c(RED, f"✗ Cannot connect to PostgreSQL at {PG_HOST}:{PG_PORT} → {e}"))
        print(c(YELLOW, "  Ensure PostgreSQL is running and credentials are correct."))
        print(c(YELLOW, f"  PG_HOST={PG_HOST} PG_PORT={PG_PORT} PG_USER={PG_USER}"))
        sys.exit(1)


def db_exists(cur, dbname):
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    return cur.fetchone() is not None


def create_db(conn, dbname):
    import psycopg2
    conn.autocommit = True
    with conn.cursor() as cur:
        safe = dbname.replace('"', '""')
        cur.execute(f'CREATE DATABASE "{safe}"')


def drop_db(conn, dbname):
    conn.autocommit = True
    import psycopg2
    with conn.cursor() as cur:
        # Terminate active connections first
        cur.execute("""
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
        """, (dbname,))
        safe = dbname.replace('"', '""')
        cur.execute(f'DROP DATABASE IF EXISTS "{safe}"')


def run_pg_schema(folder_path: Path):
    """Run pg_schema.py in the service folder to initialize tables."""
    schema_py = folder_path / "pg_schema.py"
    if not schema_py.exists():
        return False, "pg_schema.py not found"
    try:
        result = subprocess.run(
            [sys.executable, str(schema_py)],
            cwd=str(folder_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, result.stdout.strip() or "OK"
        else:
            return False, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "Timed out after 30s"
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(description="LAL10 PostgreSQL Database Setup")
    parser.add_argument("--check", action="store_true", help="Check databases without creating")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate databases (DANGER: deletes all data)")
    parser.add_argument("--schema-only", action="store_true", help="Run pg_schema.py for existing databases only")
    parser.add_argument("--service", type=str, default="", help="Only setup a specific service by name")
    args = parser.parse_args()

    services = SERVICES
    if args.service:
        services = [s for s in SERVICES if args.service.lower() in s["name"].lower()]
        if not services:
            print(c(RED, f"No service matching '{args.service}'. Available: {[s['name'] for s in SERVICES]}"))
            sys.exit(1)

    print(f"\n{c(BOLD, '═' * 58)}")
    print(f"{c(BOLD, '  🏮  LAL10 PostgreSQL Database Setup')}")
    print(f"{c(BOLD, '═' * 58)}\n")
    print(f"  Host:     {PG_HOST}:{PG_PORT}")
    print(f"  User:     {PG_USER}")
    print(f"  Services: {len(services)}")
    print()

    admin_conn = get_pg_conn("postgres")
    admin_conn.autocommit = True

    with admin_conn.cursor() as cur:
        for svc in services:
            name    = svc["name"]
            dbname  = svc["db"]
            folder  = BASE_DIR / svc["folder"]
            exists  = db_exists(cur, dbname)

            prefix = f"  {c(CYAN, name):30s} ({dbname})"

            if args.check:
                status = c(GREEN, "✔ exists") if exists else c(RED, "✗ missing")
                print(f"{prefix} → {status}")
                continue

            if args.schema_only:
                if not exists:
                    print(f"{prefix} → {c(YELLOW, 'skipped (DB missing)')}")
                    continue
                ok, msg = run_pg_schema(folder)
                status = c(GREEN, "✔ schema OK") if ok else c(RED, f"✗ schema failed: {msg[:60]}")
                print(f"{prefix} → {status}")
                continue

            if args.reset and exists:
                drop_db(admin_conn, dbname)
                exists = False
                print(f"{prefix} → {c(YELLOW, 'dropped')}")

            if not exists:
                try:
                    create_db(admin_conn, dbname)
                    print(f"{prefix} → {c(GREEN, '✔ created')}", end="")
                except Exception as e:
                    print(f"{prefix} → {c(RED, f'✗ create failed: {e}')}")
                    continue
            else:
                print(f"{prefix} → {c(GREEN, '✔ already exists')}", end="")

            # Run schema initialization
            ok, msg = run_pg_schema(folder)
            if ok:
                print(f" → {c(GREEN, 'schema OK')}")
            else:
                print(f" → {c(YELLOW, f'schema warn: {msg[:60]}')}")

    admin_conn.close()

    if not args.check and not args.schema_only:
        print(f"\n{c(BOLD, '─' * 58)}")
        print(f"{c(GREEN, '  ✔  All databases initialized!')}")
        print(f"  Run: {c(BOLD, 'bash start_all.sh')} to launch all services")
        print(f"{c(BOLD, '─' * 58)}\n")


if __name__ == "__main__":
    main()
